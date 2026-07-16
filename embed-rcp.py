# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "click",
#   "loguru",
#   "lxml>=5.0",
#   "brotli>=1.1",
#   "numpy",
#   "onnxruntime",
#   "tokenizers",
# ]
# ///
"""Optional OFFLINE pre-bake of per-drug semantic-search vectors (see CLAUDE.md).

The per-drug "Rechercher dans ce RCP" box is served by the runtime embed service
(embed-service.py), which keeps the encoder warm and (re-)embeds each page as it is
crawled. This script does the SAME work in a batch, offline, to WARM THE BACKLOG
before a first deploy (or after a bulk scrape) so readers don't wait for the
service's background sweep to reach a page. It is entirely optional: skip it and the
embed service fills the backlog on its own; skip BOTH and the box degrades to a
graceful "indisponible" note.

It is deliberately thin: it reuses the SAME warm encoder (onnx_embed.Encoder, no
torch) and the SAME one-page embed core (build.embed_page_to_vec) the service uses,
iterating build.iter_overlay_raw() (every non-empty overlay in data/rcp + data/eu,
NEVER the frozen 2022 baseline). It writes straight to the served
dist/<rcp|eu>/<slug>.vec.json with the same content hash (build.raw_hash), so its
vectors and the service's are byte-for-byte interchangeable and the shared staleness
gate treats them as one. No manifest: staleness is the src_hash baked in each
.vec.json (a re-run re-embeds only pages whose overlay text or the model changed).

Keep import-safe (``__main__`` guard) in case anything imports it later.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path

import click
from loguru import logger

HERE = Path(__file__).resolve().parent


def _load_module(filename: str, name: str):
    """Import a sibling ``foo-bar.py`` script by path (its ``-`` name isn't a valid
    import). All targets are import-safe (``__main__``-guarded)."""
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _load_module("build.py", "build")            # overlay iter + segment + write
onnx_embed = _load_module("onnx_embed.py", "onnx_embed")  # warm ONNX encoder (no torch)


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--limit", default=60, show_default=True,
              help="Max number of pages to embed this run (ignored with --all).")
@click.option("--all", "do_all", is_flag=True, help="Embed every eligible page.")
@click.option("--only", metavar="CIS", multiple=True,
              help="Embed only these CIS code(s); repeatable. Implies --force.")
@click.option("--eu/--no-eu", default=True, show_default=True,
              help="Also embed full /eu/ (EMA) pages, not just ANSM RCP pages.")
@click.option("--model-dir", default=str(onnx_embed.DEFAULT_MODEL_DIR), show_default=True,
              envvar="EMBED_MODEL_DIR",
              help="Directory of the ONNX model + tokenizer (run ./download-model.sh).")
@click.option("--intra-threads", type=int, default=4, show_default=True,
              help="onnxruntime intra-op threads for the passage encode.")
@click.option("--force", is_flag=True,
              help="Re-embed even if the content hash and model are unchanged.")
def main(limit, do_all, only, eu, model_dir, intra_threads, force):
    """Pre-bake dist/<rcp|eu>/<slug>.vec.json for crawled pages (warms the backlog)."""
    only_set = {c.strip() for c in only if c.strip()}
    if only_set:
        force = True

    logger.info("loading encoder from {}", model_dir)
    encoder = onnx_embed.Encoder(model_dir=model_dir, intra_threads=intra_threads)
    model = encoder.model_name  # same string the service bakes, so the gate agrees

    done = fresh = skipped = errors = no_page = 0
    for cis, raw, subdir in build.iter_overlay_raw():
        if only_set and cis not in only_set:
            continue
        if not eu and subdir == "eu":
            continue
        try:
            result = build.embed_page_to_vec(cis, raw, subdir, encoder,
                                             model=model, force=force)
        except Exception as exc:  # never let one bad page abort the batch
            errors += 1
            logger.warning("cis {} failed: {}", cis, exc)
            continue
        if result == "ok":
            done += 1
        elif result == "fresh":
            fresh += 1
        elif result == "no-page":
            # Overlay exists but the page isn't built yet: run `uv run build.py` first.
            no_page += 1
            continue
        if done and done % 100 == 0:
            logger.info("embedded {} pages ({} unchanged, {} not-built, {} errors)",
                        done, fresh, no_page, errors)
        # Count against the limit only pages we actually embedded this run.
        if not do_all and done >= limit:
            break

    logger.info("done: {} embedded, {} unchanged, {} not-built-yet, {} errors",
                done, fresh, no_page, errors)
    if no_page:
        logger.info("{} overlay(s) had no built page: run `uv run build.py` first",
                    no_page)


if __name__ == "__main__":
    main()
