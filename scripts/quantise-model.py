#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.11"
# dependencies = ["click", "loguru", "onnx", "onnxruntime", "sympy"]
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
  That pass needs **sympy**, which onnxruntime does not depend on, so it is listed
  above. Without it the run dies on an ImportError offering `skip_symbolic_shape=True`
  as the way out, and taking that offer is how you get a 2.3 GB "int8" graph.
- **External data has to be asked for on both sides.** A 2.38 GB model cannot be
  written into a single protobuf (the format's own limit is 2 GB), so the load and
  the save both need the external-data flag, and the result is again a stub plus a
  `.data` sibling that must travel with it.
- **Jina's pooling head is out of spec, and symbolic shape inference dies on it.**
  `Unsqueeze`-13 says `axes` is a 1-D tensor; the exported graph feeds its two
  pooling `Unsqueeze` nodes a SCALAR (`_pool_neg1`, value -1), which onnxruntime
  happily runs and `symbolic_shape_infer` crashes on with `TypeError: len() of
  unsized object`. `_repair_unsqueeze_axes` below gives those nodes a 1-D twin of
  that tensor. It cannot simply reshape the tensor, because the same `_pool_neg1`
  is also the `indices` input of a `Gather`, where 0-d is correct and means "take
  one element and drop the axis" (that Gather is what picks the last token).

Run it through the download script rather than by hand:

    ./scripts/download-model.sh

Written by Claude Code (Opus 5).
"""

from __future__ import annotations

import os
import tempfile
from pathlib import Path

import click
from loguru import logger


def _mb(path: Path) -> float:
    """Size of a graph in MB, counting its external-data sibling when there is one.

    The sibling's name is whatever wrote it: HuggingFace exports `model.onnx` beside
    `model.onnx_data`, onnxruntime writes `model_int8.onnx` beside
    `model_int8.onnx.data`. Matching only one spelling silently reports a 570 MB
    graph as 1 MB, which is how the check in main() came to be unable to fire.
    """
    if not path.exists():
        return 0.0
    total = path.stat().st_size
    total += sum(sibling.stat().st_size for sibling in path.parent.iterdir()
                 if sibling.name.startswith(path.name) and sibling != path)
    return total / 1048576


def _repair_unsqueeze_axes(src: Path, dest: Path) -> int:
    """Copy `src` to `dest`, giving every Unsqueeze a 1-D `axes` as the spec asks.

    Only the tiny stub is rewritten: the initializers stay external, so `dest` has
    to sit beside the `_data` sibling (or a symlink to it) to be loadable.

    Returns the number of nodes repointed, 0 when the graph was already conforming.
    """
    import onnx

    model = onnx.load(str(src), load_external_data=False)
    initializers = {tensor.name: tensor for tensor in model.graph.initializer}
    twins: dict[str, str] = {}
    repointed = 0
    for node in model.graph.node:
        if node.op_type != "Unsqueeze" or len(node.input) < 2:
            continue
        axes = initializers.get(node.input[1])
        if axes is None or len(axes.dims) != 0:
            continue
        name = twins.get(axes.name)
        if name is None:
            # A twin rather than a reshape: the scalar may have other consumers for
            # which 0-d is the correct rank, and here it does (see the docstring).
            twin = onnx.TensorProto()
            twin.CopyFrom(axes)
            twin.name = f"{axes.name}_axes1d"
            twin.dims.append(1)
            model.graph.initializer.append(twin)
            name = twins[axes.name] = twin.name
        node.input[1] = name
        repointed += 1
    onnx.save(model, str(dest))
    return repointed


def _quantise(model: Path, out: Path) -> None:
    """Pre-process `model` and write its weight-only int8 form to `out`."""
    # Imported here rather than at module scope: onnxruntime.quantization pulls in
    # onnx and numpy and takes a second to import, and --help should not pay for it.
    from onnxruntime.quantization import QuantType, quantize_dynamic
    from onnxruntime.quantization.shape_inference import quant_pre_process

    # The pre-processed graph is a full second copy of the weights, so it goes to a
    # temporary directory that is removed whatever happens rather than sitting in
    # models/ afterwards. It is self-contained (quant_pre_process writes its own
    # external-data sibling beside it), so unlike the input it can live anywhere.
    with tempfile.TemporaryDirectory(prefix="quantise-", dir=str(out.parent)) as tmp:
        prepared = Path(tmp) / "prepared.onnx"
        logger.info("pre-processing (symbolic shape inference)")
        try:
            quant_pre_process(str(model), str(prepared), skip_symbolic_shape=False,
                              save_as_external_data=True, all_tensors_to_one_file=True)
        except Exception as failure:  # noqa: BLE001 - any inference bug lands here
            # Not fatal, but not free either: the MatMuls the quantiser cannot see
            # through stay float, so the check on the output size in main() is what
            # decides whether the result is worth keeping.
            logger.warning("symbolic shape inference failed ({}); retrying without it",
                           failure)
            quant_pre_process(str(model), str(prepared), skip_symbolic_shape=True,
                              save_as_external_data=True, all_tensors_to_one_file=True)

        logger.info("quantising to int8")
        quantize_dynamic(prepared, out, weight_type=QuantType.QInt8,
                         extra_options={"EnableSubgraph": True},
                         use_external_data_format=True)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--src", type=click.Path(exists=True, dir_okay=False, path_type=Path),
              required=True, help="The fp32 graph to read (its _data sibling is implied).")
@click.option("--out", type=click.Path(dir_okay=False, path_type=Path), required=True,
              help="Where to write the int8 graph.")
@click.option("--force", is_flag=True, help="Quantise again even if --out already exists.")
def main(src: Path, out: Path, force: bool) -> None:
    """Weight-only int8 quantisation of an ONNX encoder."""
    if out.exists() and not force:
        logger.info("{} already exists ({:.0f} MB); pass --force to redo it", out, _mb(out))
        return
    out.parent.mkdir(parents=True, exist_ok=True)
    logger.info("reading {} ({:.0f} MB)", src, _mb(src))

    # The repaired stub is a stub: it points at the original weights by a RELATIVE
    # name, so it has to live in the same directory as them. Reaching them from a
    # temporary directory elsewhere is not an option, because onnx inspects the file
    # it is about to read and refuses both a symlink and a hard link ("potential
    # hardlink attack"), and copying 2.38 GB to dodge that would be worse than the
    # 1.2 MB written here. It is removed again whatever happens.
    handle, name = tempfile.mkstemp(prefix="repaired-", suffix=".onnx", dir=str(src.parent))
    os.close(handle)
    repaired = Path(name)
    try:
        repointed = _repair_unsqueeze_axes(src, repaired)
        if repointed:
            logger.info("repaired {} Unsqueeze node(s) whose axes were 0-d", repointed)
        _quantise(repaired, out)
    finally:
        repaired.unlink(missing_ok=True)

    # An int8 graph is about a quarter of its fp32 input. Anything close to the
    # original size means the quantiser reached almost nothing, which is a silent
    # failure otherwise: the file is there, the service loads it, and the VPS pays
    # for weights nobody asked for.
    before, after = _mb(src), _mb(out)
    if after > 0.6 * before:
        logger.warning("{} is {:.0f} MB against {:.0f} MB of fp32: the quantiser "
                       "reached little of the graph. Expect about a quarter.",
                       out, after, before)

    logger.success("{} written ({:.0f} MB, from {:.0f} MB)", out, _mb(out), _mb(src))
    logger.info("Next: restart the embed service so it loads the new weights, then "
                "re-bake every vector. The model name is part of the cache key, so "
                "nothing is reused across a model change.")


if __name__ == "__main__":
    main()
