# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "click",
#   "httpx",
#   "loguru",
#   "lxml>=5.0",
#   "brotli>=1.1",
#   "numpy",
#   "sentence-transformers>=3.0",
# ]
# ///
"""Build-time embeddings for per-drug semantic search (see TODO.md / CLAUDE.md).

OPTIONAL, offline, run occasionally (like scrape-rcp.py). For every drug that
renders an RCP page it segments the cleaned RCP into per-section chunks (shared
``build.section_chunks``, so the chunks line up with the ``sec-N`` anchors the page
bakes), embeds each chunk with a multilingual sentence encoder, int8-quantises the
vectors (``build.quantize_int8``) and writes one sidecar per drug at
``data/emb/<cis>.json[.gz]``. ``build.py`` later repackages those into the served
``dist/rcp/<slug>.vec.json`` the browser fetches on demand; NOTHING here touches
dist or the network at serve time (the model runs here, not in the browser build).

This is the counterpart of scrape-rcp.py: it reuses that script's tolerant
manifest IO (``load_manifest``/``save_manifest``, with the read-only-rootfs
fallback) via its own manifest ``data/.embed-manifest.json`` so the embed TTL/hash
state never collides with the scrape one, and it iterates exactly the pages the
build renders through ``build.iter_rcp_raw``. Incremental: a CIS is re-embedded
only when its raw RCP HTML or the model changed (content hash in the manifest),
so a re-run on unchanged data is cheap.

Keep import-safe (``__main__`` guard) in case anything imports it later.
"""

from __future__ import annotations

import base64
import gzip
import hashlib
import importlib.util
import json
import os
import struct
from datetime import datetime, timezone
from pathlib import Path

import click
from loguru import logger

HERE = Path(__file__).resolve().parent
DATA = HERE / "data"
EMB_OVERLAY_DIR = DATA / "emb"
EMB_MANIFEST_PATH = DATA / ".embed-manifest.json"

# Default multilingual encoder. XLM-RoBERTa family, 384-dim, contrastively trained
# for retrieval (query->passage), strong on French; MIT. The runtime loads the
# matching ONNX build (Xenova/multilingual-e5-small) via transformers.js. e5 wants
# "query:"/"passage:" prefixes; _prefixes() derives them from the model name.
DEFAULT_MODEL = os.environ.get("EMBED_MODEL", "intfloat/multilingual-e5-small")
# Gzip sidecars by default (base64 int8 + JSON compress ~25%); flip with --no-gzip
# or EMBED_OVERLAY_GZIP=0. build.py reads either format transparently.
DEFAULT_GZIP = os.environ.get("EMBED_OVERLAY_GZIP", "1") not in ("0", "false", "no")


def _load_module(filename: str, name: str):
    """Import a sibling ``foo-bar.py`` script by path (its ``-`` name isn't a valid
    import). Both targets are import-safe (``__main__``-guarded)."""
    spec = importlib.util.spec_from_file_location(name, HERE / filename)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


build = _load_module("build.py", "build")          # records iter + segmentation + quant
scrape = _load_module("scrape-rcp.py", "scrape_rcp")  # tolerant manifest IO (reused)


def _prefixes(model: str) -> tuple[str, str]:
    """(query_prefix, passage_prefix) for the model. e5 models require these; most
    sentence-transformers models use none."""
    if "e5" in model.lower():
        return "query: ", "passage: "
    return "", ""


def _raw_hash(raw: str) -> str:
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def _sidecar_paths(cis: str) -> tuple[Path, Path]:
    return EMB_OVERLAY_DIR / f"{cis}.json", EMB_OVERLAY_DIR / f"{cis}.json.gz"


def write_sidecar(cis: str, payload: dict, gzip_sidecar: bool) -> Path:
    """Write one drug's embedding sidecar atomically (temp + rename); return path.

    Mirrors scrape-rcp.write_overlay's atomic write + one-format-per-CIS rule, but
    for a ``<cis>.json[.gz]`` JSON payload instead of the HTML overlay (different
    suffix, no zero-byte sentinel), so it cannot reuse write_overlay directly.
    """
    EMB_OVERLAY_DIR.mkdir(parents=True, exist_ok=True)
    data = json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")
    plain, gz = _sidecar_paths(cis)
    dest, sibling = (gz, plain) if gzip_sidecar else (plain, gz)
    tmp = dest.with_name(dest.name + ".tmp")
    tmp.write_bytes(gzip.compress(data) if gzip_sidecar else data)
    tmp.replace(dest)
    sibling.unlink(missing_ok=True)  # never leave both formats for one CIS
    return dest


def _sidecar_exists(cis: str) -> bool:
    plain, gz = _sidecar_paths(cis)
    return plain.exists() or gz.exists()


def _encode_chunk_payload(model, chunks, passage_prefix: str, query_prefix: str) -> dict:
    """Embed a drug's chunks and build the sidecar dict.

    chunks: list of (sec_id, snippet, chunk_text) from build.section_chunks.
    """
    texts = [passage_prefix + text for _, _, text in chunks]
    vecs = model.encode(
        texts, normalize_embeddings=True, convert_to_numpy=True, batch_size=64,
        show_progress_bar=False,
    )
    dim = int(vecs.shape[1])
    out_chunks = []
    for (sec_id, snippet, _), vec in zip(chunks, vecs):
        q = build.quantize_int8(vec.tolist())  # single source of the int8 formula
        b64 = base64.b64encode(struct.pack(f"{len(q)}b", *q)).decode("ascii")
        out_chunks.append({"sec": sec_id, "snippet": snippet, "q": b64})
    return {
        "model": DEFAULT_MODEL,
        "dim": dim,
        "query_prefix": query_prefix,
        "chunks": out_chunks,
    }


@click.command(context_settings={"help_option_names": ["-h", "--help"]})
@click.option("--limit", default=60, show_default=True,
              help="Max number of drugs to embed this run (ignored with --all).")
@click.option("--all", "do_all", is_flag=True, help="Embed every eligible drug.")
@click.option("--only", metavar="CIS", multiple=True,
              help="Embed only these CIS code(s); repeatable. Implies --force.")
@click.option("--model", default=DEFAULT_MODEL, show_default=True,
              help="sentence-transformers model id.")
@click.option("--gzip/--no-gzip", "gzip_sidecar", default=DEFAULT_GZIP, show_default=True,
              help="Gzip the sidecars (env EMBED_OVERLAY_GZIP).")
@click.option("--force", is_flag=True,
              help="Re-embed even if the content hash and model are unchanged.")
def main(limit, do_all, only, model, gzip_sidecar, force):
    """Embed RCP sections into data/emb/<cis>.json[.gz] for client-side search."""
    global DEFAULT_MODEL
    DEFAULT_MODEL = model
    query_prefix, passage_prefix = _prefixes(model)
    manifest = scrape.load_manifest(EMB_MANIFEST_PATH)
    only_set = {c.strip() for c in only if c.strip()}
    if only_set:
        force = True

    # Lazy, heavy import so --help stays instant and build.py import isn't burdened.
    logger.info("loading model {} (first run downloads it)", model)
    from sentence_transformers import SentenceTransformer

    encoder = SentenceTransformer(model)

    done = errors = skipped = 0
    for cis, raw, _asof in build.iter_rcp_raw():
        if only_set and cis not in only_set:
            continue
        entry = manifest.get(cis)
        fresh = (
            not force
            and entry is not None
            and entry.get("raw") == _raw_hash(raw)
            and entry.get("model") == model
            and _sidecar_exists(cis)
        )
        if fresh:
            skipped += 1
            continue
        try:
            chunks = build.section_chunks(raw, cis)
            if not chunks:
                # No titled sections: write nothing, but record so we don't retry.
                manifest[cis] = {
                    "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    "raw": _raw_hash(raw), "model": model, "n": 0,
                }
                continue
            payload = _encode_chunk_payload(encoder, chunks, passage_prefix, query_prefix)
            write_sidecar(cis, payload, gzip_sidecar)
            manifest[cis] = {
                "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds"),
                "raw": _raw_hash(raw), "model": model, "n": len(chunks),
            }
            done += 1
        except Exception as exc:  # never let one bad drug abort the batch
            errors += 1
            logger.warning("cis {} failed: {}", cis, exc)
            manifest[cis] = {"status": "error", "model": model,
                             "last_run": datetime.now(timezone.utc).isoformat(timespec="seconds")}

        if done and done % 100 == 0:
            scrape.save_manifest(manifest, EMB_MANIFEST_PATH)  # checkpoint
            logger.info("embedded {} drugs ({} skipped, {} errors)", done, skipped, errors)
        if not do_all and done >= limit:
            break

    scrape.save_manifest(manifest, EMB_MANIFEST_PATH)
    logger.info("done: {} embedded, {} unchanged, {} errors -> {}",
                done, skipped, errors, EMB_OVERLAY_DIR)
    logger.info("now rebuild: uv run build.py")


if __name__ == "__main__":
    main()
