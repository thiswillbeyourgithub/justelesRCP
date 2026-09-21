#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click", "loguru", "onnx", "onnxruntime"]
# ///
"""Quantise an fp32 ONNX encoder to int8, in place in the models directory.

Why this exists: `onnx_embed.RUNTIME_MODEL` is
`jinaai/jina-embeddings-v5-text-small-retrieval`, and Jina publishes that graph in
fp32 ONLY (`onnx/model.onnx`, a 1.3 MB stub, plus `onnx/model.onnx_data`, 2.38 GB
of weights beside it). The previous encoder, arctic-embed-l-v2.0, shipped an int8
graph on the hub and this step did not exist. The VPS has little RAM and no GPU, so
fp32 is not an option there: 2.38 GB of weights resident, against about 650 MB for
the int8 graph this writes.

What it does is `quantize_dynamic`, which is weight-only: the weights are stored as
int8 and the activations stay float, computed per operator at run time. That is the
same recipe arctic's published `model_int8.onnx` was made with, so the runtime shape
is unchanged; `onnx_embed` loads whatever `_profile` names and knows nothing about
how it was produced.

Two things that cost hours if got wrong:

- **Pre-processing is not optional.** `quantize_dynamic` on a raw transformer graph
  leaves the MatMuls it cannot see through in fp32, and the output is barely
  smaller than the input. `onnxruntime.quantization.shape_inference.quant_pre_process`
  runs symbolic shape inference first, which is what lets the quantiser reach them.
- **External data has to be asked for on both sides.** A 2.38 GB model cannot be
  written into a single protobuf (the format's own limit is 2 GB), so the load and
  the save both need the external-data flag, and the result is again a stub plus a
  `.data` sibling that must travel with it.

Run it through the download script rather than by hand:

    ./scripts/download-model.sh

Written by Claude Code (Opus 5).
"""

from __future__ import annotations

import shutil
import tempfile
from pathlib import Path

import click
from loguru import logger


def _mb(path: Path) -> float:
    """Size of a graph in MB, counting its external-data sibling when there is one."""
    total = path.stat().st_size if path.exists() else 0
    data = path.with_name(path.name + "_data")
    if data.exists():
        total += data.stat().st_size
    return total / 1048576


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--src", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              required=True, help="The fp32 graph to read (its _data sibling is implied).")
@click.option("--out", type=click.Path(dir_okay=False, path_type=Path), required=True,
              help="Where to write the int8 graph.")
@click.option("--force", is_flag=True, help="Quantise again even if --out already exists.")
def main(src: Path, out: Path, force: bool) -> None:
    """Weight-only int8 quantisation of an ONNX encoder."""
    # Imported here rather than at module scope: onnxruntime.quantization pulls in
    # onnx and numpy and takes a second to import, and --help should not pay for it.
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from onnxruntime.quantization.shape_inference import quant_pre_process

    if out.exists() and not force:
        logger.info("{} already exists ({:.0f} MB); pass --force to redo it", out, _mb(out))
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    logger.info("reading {} ({:.0f} MB)", src, _mb(src))

    # The pre-processed graph is a full second copy of the weights, so it goes to a
    # temporary directory that is removed whatever happens rather than sitting in
    # models/ afterwards. The name matters: the external-data sibling is resolved
    # relative to the stub, so the two have to be written side by side.
    with tempfile.TemporaryDirectory(prefix="quantise-", dir=str(out.parent)) as tmp:
        prepared = Path(tmp) / "prepared.onnx"
        logger.info("pre-processing (symbolic shape inference)")
        quant_pre_process(str(src), str(prepared), skip_symbolic_shape=False,
                          save_as_external_data=True, all_tensors_to_one_file=True)
        logger.info("quantising to int8")
        quantize_dynamic(prepared, out, weight_type=QuantType.QInt8,
                         extra_options={"EnableSubgraph": True},
                         use_external_data_format=True)

    logger.success("{} written ({:.0f} MB, from {:.0f} MB)", out, _mb(out), _mb(src))
    logger.info("Next: restart the embed service so it loads the new weights, then "
                "re-bake every vector. The model name is part of the cache key, so "
                "nothing is reused across a model change.")


if __name__ == "__main__":
    main()
