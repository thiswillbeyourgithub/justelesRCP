# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "click",
#   "loguru",
#   "lxml>=5.0",
#   "brotli>=1.1",
#   "numpy",
#   "onnxruntime-gpu",
#   "tokenizers",
#   "tqdm",
# ]
# ///
# NOTE: this OFFLINE tool depends on onnxruntime-GPU (not the CPU-only onnxruntime the
# VPS embed-service uses) so it can bake vectors on a local GPU when one is present. The
# GPU wheel also runs fine CPU-only (the CUDA provider just won't register), so a
# machine without a GPU degrades gracefully; --no-gpu forces CPU. Using a GPU needs an
# NVIDIA driver + the CUDA 12 runtime + cuDNN 9 the installed onnxruntime-gpu expects.
# If cuDNN is missing you'll see "libcudnn.so.9: cannot open shared object file" and it
# falls back to CPU (logged). Two no-root ways to supply them without a system install:
#   * add the pip wheels for THIS run (uv puts them in the same env; _preload_cuda_libs
#     below then makes onnxruntime find them):
#       uv run --with nvidia-cudnn-cu12 --with nvidia-cublas-cu12 \
#              --with nvidia-cuda-runtime-cu12 --with nvidia-cufft-cu12 \
#              --with nvidia-curand-cu12 src/embed-rcp.py --all --batch-size 128
#   * or install cuDNN 9 for CUDA 12 system-wide (e.g. apt: libcudnn9-cuda-12).
# They're kept OUT of the deps above on purpose: a pure-CPU run must not pull ~1.5 GB of
# CUDA wheels, and a bad pin must not break the working CPU fallback.
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
import random
from pathlib import Path

import click
import onnxruntime as ort
from loguru import logger
from tqdm import tqdm

HERE = Path(__file__).resolve().parent


def _preload_cuda_libs() -> None:
    """Make the CUDA/cuDNN shared libraries from the ``nvidia-*-cu12`` pip wheels loadable
    by onnxruntime's CUDA provider. Its ``.so`` does NOT search ``site-packages`` on its
    own, so wheels installed via ``uv run --with nvidia-cudnn-cu12 ...`` are present on
    disk yet invisible to it (the "libcudnn.so.9: cannot open shared object file" failure).
    onnxruntime>=1.21 exposes ``preload_dlls()`` which loads them from the nvidia packages;
    on older builds we ctypes-preload the wheels' libs ``RTLD_GLOBAL`` in dependency order
    (cudart/cublas before cudnn) so the provider's later ``dlopen`` resolves their symbols.
    A no-op when neither the wheels nor system libs are present (the provider then just
    fails to register and we fall back to CPU, already handled). Must run BEFORE onnxruntime
    probes CUDA (``get_available_providers``/session build), so ``main`` calls it first."""
    preload = getattr(ort, "preload_dlls", None)
    if callable(preload):
        try:
            preload()  # official path: loads CUDA + cuDNN from the nvidia-*-cu12 wheels
            return
        except Exception as exc:  # pragma: no cover - depends on ort version/env
            logger.debug("ort.preload_dlls() failed ({}); trying manual preload", exc)
    import ctypes
    import glob
    import site

    bases = list(site.getsitepackages())
    user = site.getusersitepackages()
    if user:
        bases.append(user)
    lib_dirs: list[str] = []
    for base in dict.fromkeys(bases):  # de-dup, keep order
        lib_dirs.extend(glob.glob(str(Path(base) / "nvidia" / "*" / "lib")))
    # Load order matters: cudnn needs cudart + cublas, so pull those in first, each
    # RTLD_GLOBAL so the CUDA provider's own dlopen later sees their symbols.
    for pattern in ("libcudart.so*", "libcublasLt.so*", "libcublas.so*",
                    "libcufft.so*", "libcurand.so*", "libcudnn*.so*"):
        for libdir in lib_dirs:
            for so in sorted(glob.glob(str(Path(libdir) / pattern))):
                try:
                    ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
                except OSError:  # pragma: no cover - best effort
                    pass


def _select_providers(want_gpu: bool) -> tuple[list[str], list[str]]:
    """Pick onnxruntime execution providers. With ``want_gpu``, prefer a GPU provider
    (CUDA, then ROCm) IF the installed onnxruntime exposes it, always appending CPU as a
    fallback so an unsupported int8 op or an absent GPU degrades instead of erroring.
    Returns ``(chosen, available)`` for logging."""
    available = list(ort.get_available_providers())
    if want_gpu:
        for gpu in ("CUDAExecutionProvider", "ROCMExecutionProvider"):
            if gpu in available:
                return [gpu, "CPUExecutionProvider"], available
    return ["CPUExecutionProvider"], available


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
              help="Directory of the ONNX model + tokenizer (run ./scripts/download-model.sh).")
@click.option("--intra-threads", type=int, default=4, show_default=True,
              help="onnxruntime intra-op threads for the passage encode (CPU path).")
@click.option("--gpu/--no-gpu", default=True, show_default=True,
              help="Use a GPU (CUDA/ROCm) if onnxruntime exposes one; else fall back to "
                   "CPU. --no-gpu forces CPU.")
@click.option("--batch-size", type=int, default=32, show_default=True,
              help="Passage encode batch size; raise (e.g. 128) to feed a GPU better.")
@click.option("--force", is_flag=True,
              help="Re-embed even if the content hash and model are unchanged.")
def main(limit, do_all, only, eu, model_dir, intra_threads, gpu, batch_size, force):
    """Pre-bake dist/<rcp|eu>/<slug>.vec.json for crawled pages (warms the backlog)."""
    only_set = {c.strip() for c in only if c.strip()}
    if only_set:
        force = True

    # Make any nvidia-*-cu12 wheels (uv --with) findable BEFORE onnxruntime probes CUDA,
    # else the CUDA provider can't load them and never registers (see _preload_cuda_libs).
    if gpu:
        _preload_cuda_libs()
    providers, available = _select_providers(gpu)
    if gpu and providers[0] == "CPUExecutionProvider":
        logger.warning("no GPU execution provider available (have: {}); using CPU. For "
                       "NVIDIA you need the driver + CUDA 12 + cuDNN 9; a missing "
                       "'libcudnn.so.9' means cuDNN is absent. No-root fix: re-run with "
                       "`uv run --with nvidia-cudnn-cu12 --with nvidia-cublas-cu12 "
                       "--with nvidia-cuda-runtime-cu12 --with nvidia-cufft-cu12 "
                       "--with nvidia-curand-cu12 src/embed-rcp.py ...` (or install cuDNN "
                       "9 system-wide). Otherwise `--no-gpu --intra-threads N` is fine.",
                       ", ".join(available))

    logger.info("loading encoder from {} (~500 MB int8 weights, takes a moment)", model_dir)
    encoder = onnx_embed.Encoder(model_dir=model_dir, intra_threads=intra_threads,
                                 providers=providers, passage_batch_size=batch_size)
    model = encoder.model_name  # same string the service bakes, so the gate agrees
    # get_providers() reports what actually registered, so a silent GPU-load failure
    # (CUDA libs missing) is visible: it will read CPUExecutionProvider only.
    logger.info("execution provider(s): {} | batch={}",
                ", ".join(encoder.session.get_providers()), batch_size)

    # Progress-bar total: the path-level overlay count (a cheap dir scan, no content
    # reads). It slightly over-counts what actually embeds (iter_overlay_raw skips
    # zero-byte archived overlays), so the bar may finish a hair under 100%; close
    # enough to show position + ETA. Each page's .vec.json is written as it is embedded,
    # so a Ctrl-C is safe and a re-run resumes (unchanged pages are skipped as "fresh").
    # SHUFFLE the overlay order: /eu/ (long EMA SmPCs) and /rcp/ (short) pages are grouped
    # by lane on disk, so a lane-ordered walk makes tqdm's smoothed rate/ETA swing wildly
    # (all-fast then all-slow). Interleaving long + short randomly keeps the running
    # per-page cost representative from early on, so the ETA is trustworthy sooner.
    paths = list(build.iter_overlay_paths())
    random.shuffle(paths)
    total = len(paths)
    logger.info("encoder ready; {} overlay(s) to consider{}", total,
                "" if do_all else f", stopping after {limit} embedded")

    done = fresh = skipped = errors = no_page = 0
    bar = tqdm(build.iter_overlay_raw(paths), total=total, unit="page",
               desc="embedding", smoothing=0.05)
    for cis, raw, subdir in bar:
        if only_set and cis not in only_set:
            continue
        if not eu and subdir == "eu":
            continue
        try:
            result = build.embed_page_to_vec(cis, raw, subdir, encoder,
                                             model=model, force=force)
        except Exception as exc:  # never let one bad page abort the batch
            errors += 1
            bar.write(f"cis {cis} failed: {exc}")  # write() keeps the bar intact
            continue
        if result == "ok":
            done += 1
        elif result == "fresh":
            fresh += 1
        elif result == "no-page":
            # Overlay exists but the page isn't built yet: run `uv run build.py` first.
            no_page += 1
            continue
        # Live counters in the bar's postfix (redrawn on tqdm's own schedule).
        bar.set_postfix(embedded=done, unchanged=fresh, notbuilt=no_page,
                        err=errors, refresh=False)
        # Count against the limit only pages we actually embedded this run.
        if not do_all and done >= limit:
            break
    bar.close()

    logger.info("done: {} embedded, {} unchanged, {} not-built-yet, {} errors",
                done, fresh, no_page, errors)
    if no_page:
        logger.info("{} overlay(s) had no built page: run `uv run build.py` first",
                    no_page)


if __name__ == "__main__":
    main()
