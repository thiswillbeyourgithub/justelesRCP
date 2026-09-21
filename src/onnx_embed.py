# /// script
# requires-python = ">=3.11"
# dependencies = [
#   "onnxruntime",
#   "tokenizers",
#   "numpy",
# ]
# ///
"""Warm, self-hosted ONNX sentence encoder (see CLAUDE.md / the semantic-search plan).

This is the SINGLE encoder shared by the two server-side consumers of per-drug
semantic search:

- ``embed-service.py`` (the runtime container): keeps one ``Encoder`` resident and
  uses it for BOTH the background page-passage embedding and the live query
  embedding, so a reader's query never downloads a model (the old browser design) and
  the compute stays on the server.
- ``embed-rcp.py`` (optional offline pre-bake): the same encoder, so offline and
  online vectors come from identical weights and can never disagree.

Deliberately depends on ``onnxruntime`` + ``tokenizers`` ONLY (NOT torch /
sentence-transformers): that is the difference between a ~300 MB and a ~2 GB image,
and it lets the hardened, read-only runtime container stay tiny. It runs the int8
``jinaai/jina-embeddings-v5-text-small-retrieval`` ONNX weights (last-token pooled,
L2-normalised, MRL-truncated to 1024 dims, i.e. its full width), driven by a
per-model recipe (``_profile``) so the query
and passage sides always share one backend + one set of weights.

Pure + import-safe (``__main__`` guard); no network, and no filesystem writes except
the one ``ensure_cudnn_visible`` makes on demand (a symlink under the temp dir, only
when an offline GPU bake asks for it; the runtime service never calls it).
"""

from __future__ import annotations

import hashlib
import json
import os
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

HERE = Path(__file__).resolve().parent

# The runtime model. MUST match the vectors baked into every .vec.json (a query vector
# and the passage vectors have to come from the same weights), and the model
# scripts/download-model.sh fetches. Changing it re-embeds the whole catalog (build.read_vec_meta
# gates on this name). Mounted read-only into the container from ./models by
# scripts/download-model.sh; NOT served to browsers.
RUNTIME_MODEL = "jinaai/jina-embeddings-v5-text-small-retrieval"
# models/ lives at the repo root (this script is in src/), and is mounted at
# ``/app/models`` in the embed container (HERE = ``/app/src`` there); parent resolves
# both. EMBED_MODEL_DIR overrides it in the container anyway.
DEFAULT_MODEL_DIR = HERE.parent / "models" / RUNTIME_MODEL


def _profile(model_name: str) -> dict:
    """Per-model runtime recipe, keyed by a substring of the name so a swap only touches
    RUNTIME_MODEL (+ the matching scripts/download-model.sh fetch). Fields:
      onnx    : the int8 ONNX file under <model_dir>/onnx/ to load
      pooling : "cls" (first token; arctic-embed v2.0 / XLM-R lineage), "mean" (e5)
                or "last" (final unmasked token; jina-embeddings-v5 / Qwen3 lineage)
      query   : prefix prepended to a QUERY before tokenising
      passage : prefix prepended to a DOCUMENT/passage (arctic: NONE; e5: "passage: ")
      out_dim : Matryoshka (MRL) truncation length, or None to keep the full width

    arctic-embed-l-v2.0: CLS-pool -> L2-normalise, query-only "query: " prefix, MRL to
    256 (truncate THEN normalise once; the ST pipeline's pre-truncation normalise is a
    mathematical no-op). Verified against the repo's 1_Pooling/config.json +
    config_sentence_transformers.json + the ONNX graph (inputs input_ids/attention_mask
    only, output token_embeddings [B,L,1024])."""
    n = model_name.lower()
    if "jina-embeddings-v5" in n:
        # Qwen3-0.6B lineage: LAST-token pooling, and both sides carry a prefix
        # ("Query: " / "Document: "), where arctic prefixed the query only. The
        # retrieval repo is the one with the adapter merged into the weights, which
        # is what makes a plain onnxruntime session enough. Its published ONNX is
        # fp32 (2.38 GB, external data); model_int8.onnx is produced locally by
        # scripts/quantise-model.py, which is why the name is not on the hub.
        return {"onnx": "model_int8.onnx", "pooling": "last",
                "query": "Query: ", "passage": "Document: ", "out_dim": 1024}
    if "arctic-embed" in n:
        return {"onnx": "model_int8.onnx", "pooling": "cls",
                "query": "query: ", "passage": "", "out_dim": 1024}
    if "e5" in n:
        return {"onnx": "model_quantized.onnx", "pooling": "mean",
                "query": "query: ", "passage": "passage: ", "out_dim": None}
    # Unknown model: safe defaults (mean pool, no prefixes, full width, common ONNX name).
    return {"onnx": "model_quantized.onnx", "pooling": "mean",
            "query": "", "passage": "", "out_dim": None}


# --- CUDA / cuDNN plumbing for the OFFLINE bakes -------------------------------
# These three live here, in the module BOTH projects already import, rather than in
# either bake script. justelesrecos's scripts/embed.py and this repo's src/embed-rcp.py
# had byte-identical copies of the first two, with a comment in the former saying the
# clean fix was to move them here: its dependencies (onnxruntime, tokenizers, numpy) are
# exactly what they need, while importing embed-rcp.py would drag in build.py and the
# crawler's lxml + brotli. The runtime embed service imports this module too and simply
# never calls them (it is CPU-only by design), which costs nothing: they do no work at
# import time.
#
# ``log`` is an optional callable taking one string. loguru is NOT a dependency here, so
# each caller passes its own logger and a silent default keeps this module importable in
# the container.


def batch_feed(tokenizer, texts: list[str], *, max_len: int,
               token_type_ids: bool = False) -> dict:
    """Tokenise one batch into the arrays the ONNX graph takes.

    Parameters
    ----------
    tokenizer : tokenizers.Tokenizer
        The model's tokenizer, used as configured (its `tokenizer.json` decides
        padding and truncation).
    texts : list of str
        The batch, already carrying whatever prefix the model wants.
    max_len : int
        Hard ceiling on tokens per row, applied after the tokenizer.
    token_type_ids : bool, optional
        Whether the graph declares a `token_type_ids` input; when it does, a zero
        array of the right shape is added.

    Returns
    -------
    dict
        `input_ids` and `attention_mask` as int64 arrays of shape (rows, seq_len),
        plus `token_type_ids` when asked for.

    Notes
    -----
    The mask comes from the tokenizer, and that is the whole reason this is a
    function. `tokenizer.json` ships with padding ENABLED (`pad_id` 1, direction
    right), so `encode_batch` already pads every row to the batch's longest, and
    the code here used to build its own mask as "1 for the first len(ids)
    positions". After the tokenizer had padded, len(ids) WAS the padded length, so
    every pad token was declared a real token and the transformer attended to a run
    of `<pad>`. Nothing failed: the vectors just quietly depended on which other
    passages happened to share the batch.

    Measured on 512 corpus chunks, batch 1 against batch 64, cosine of a passage
    against itself: 0.987 mean and 0.920 worst with the old mask, 0.99999+ with
    this one. The bake ran at batch 64 and the query service encodes one string at
    a time, where nothing is padded, so the defect fell exactly on the gap between
    the two sides it is most important to keep identical.

    Padding value: rows are filled with the tokenizer's own `pad_id` rather than 0,
    which for XLM-R is `<s>` and not `<pad>`. With a correct mask this measured no
    difference at all (0.999998 either way in fp16), so it is written for the
    reader rather than for the arithmetic.
    """
    encs = tokenizer.encode_batch(texts)
    ids_rows = [e.ids[:max_len] for e in encs]
    mask_rows = [e.attention_mask[:max_len] for e in encs]
    seq_len = max((len(row) for row in ids_rows), default=1) or 1
    padding = tokenizer.padding or {}
    input_ids = np.full((len(ids_rows), seq_len), padding.get("pad_id", 0), dtype=np.int64)
    attention = np.zeros((len(ids_rows), seq_len), dtype=np.int64)
    for row, (ids, mask) in enumerate(zip(ids_rows, mask_rows)):
        input_ids[row, : len(ids)] = ids
        attention[row, : len(mask)] = mask
    feed = {"input_ids": input_ids, "attention_mask": attention}
    if token_type_ids:
        feed["token_type_ids"] = np.zeros_like(input_ids)
    return feed


def _nvidia_lib_dirs() -> list[Path]:
    """Find the `lib` directories of any installed nvidia-*-cu12 wheels.

    Returns
    -------
    list of Path
        Every `<somewhere>/nvidia/<component>/lib` directory reachable from this
        interpreter, in import order and de-duplicated. Empty when no wheels are
        installed, which is the normal case on a machine with system CUDA.

    Notes
    -----
    `sys.path`, not `site.getsitepackages()`, and this is the whole point of the
    function. `uv run --with nvidia-cudnn-cu12 script.py` does NOT install into the
    script's own environment: it layers a second environment and puts it on
    `sys.path`, so `site.getsitepackages()` returns one directory that does not
    contain `nvidia/` at all. Searching only that directory made both callers below
    decide there were no wheels and return quietly, and the run then died several
    seconds later inside the first inference with "dlopen failed for libcudnn.so",
    which reads like a missing driver rather than a search-path bug.

    Site directories are appended anyway for the plain `pip install` case, where
    they are already on `sys.path` too but need not be by the time this runs.
    """
    import glob
    import site
    import sys

    bases: list[str] = [entry for entry in sys.path if entry]
    bases.extend(entry for entry in site.getsitepackages() if entry)
    user = site.getusersitepackages()
    if user:
        bases.append(user)
    found: list[Path] = []
    for base in dict.fromkeys(bases):  # de-duplicate, keep order
        found.extend(Path(d) for d in sorted(glob.glob(str(Path(base) / "nvidia" / "*" / "lib"))))
    return list(dict.fromkeys(found))


def preload_cuda_libs(log=None) -> None:
    """Make the CUDA and cuDNN libraries from the ``nvidia-*-cu12`` wheels loadable.

    Parameters
    ----------
    log : callable, optional
        Called with one diagnostic string when the official preload path fails.

    Notes
    -----
    onnxruntime's ``.so`` does not search ``site-packages``, so wheels installed with
    ``uv run --with nvidia-cudnn-cu12 ...`` are present on disk yet invisible to it,
    which surfaces as "libcudnn.so.9: cannot open shared object file" and a silent fall
    back to CPU. onnxruntime >= 1.21 exposes ``preload_dlls()``; on older builds the
    wheels' libraries are ctypes-loaded RTLD_GLOBAL in dependency order (cudart and
    cublas before cudnn) so the provider's later ``dlopen`` resolves their symbols. A
    no-op when neither wheels nor system libraries are present. Must run BEFORE
    onnxruntime probes CUDA.
    """
    preload = getattr(ort, "preload_dlls", None)
    if callable(preload):
        try:
            preload()
            return
        except Exception as exc:  # pragma: no cover - depends on ort version/env
            if log:
                log(f"ort.preload_dlls() failed ({exc}); trying a manual preload")
    import ctypes
    import glob

    lib_dirs = _nvidia_lib_dirs()
    for pattern in ("libcudart.so*", "libcublasLt.so*", "libcublas.so*",
                    "libcufft.so*", "libcurand.so*", "libcudnn*.so*"):
        for lib_dir in lib_dirs:
            for so in sorted(glob.glob(str(Path(lib_dir) / pattern))):
                try:
                    ctypes.CDLL(so, mode=ctypes.RTLD_GLOBAL)
                except OSError:  # pragma: no cover - best effort
                    pass


def ensure_cudnn_visible(log=None) -> None:
    """Re-execute this process with LD_LIBRARY_PATH set, if that is what CUDA needs.

    Parameters
    ----------
    log : callable, optional
        Called with one string just before the re-exec, so the operator sees why the
        process restarted.

    Notes
    -----
    onnxruntime 1.30 ``dlopen``s the UNVERSIONED soname ``libcudnn.so``, while the
    ``nvidia-cudnn-cu12`` wheel ships only ``libcudnn.so.9``. The failure is late and
    confusing: providers list CUDA, the session builds, and the first inference dies with
    "NOT_IMPLEMENTED ... ReduceL2 ... cuDNN is unavailable for CUDA Execution Provider:
    dlopen failed for libcudnn.so".

    ctypes-preloading the versioned file does not fix it, because ``dlopen`` matches on
    the exact name (an already-loaded object answers to its SONAME, ``libcudnn.so.9``,
    not to the name asked for), and the loader reads LD_LIBRARY_PATH once at process
    start, so setting it from Python is too late. Hence a re-exec: build a directory of
    symlinks whose names are the ones onnxruntime asks for, put it and the wheels' own
    library directories on LD_LIBRARY_PATH, and start over. The marker variable stops
    that from recursing.

    Keeping this in the code rather than in a shell wrapper is deliberate: it is
    environment plumbing that must travel with the code, or the next person rediscovers
    a multi-hour job silently running on CPU.
    """
    import glob
    import sys
    import tempfile

    if os.environ.get("JLR_CUDNN_REEXEC") == "1":
        return
    lib_dirs = _nvidia_lib_dirs()
    versioned = [so for lib_dir in lib_dirs
                 for so in sorted(glob.glob(str(lib_dir / "libcudnn.so.*")))]
    if not versioned:
        return  # no wheels: either cuDNN is installed system-wide, or CPU it is

    compat = Path(tempfile.gettempdir()) / "jlr-cudnn-compat"
    compat.mkdir(parents=True, exist_ok=True)
    link = compat / "libcudnn.so"
    target = Path(sorted(versioned)[-1])
    if link.is_symlink() or link.exists():
        link.unlink()
    link.symlink_to(target)

    search = os.pathsep.join([str(compat), *(str(d) for d in lib_dirs),
                              os.environ.get("LD_LIBRARY_PATH", "")]).rstrip(os.pathsep)
    if log:
        log(f"linking {link.name} -> {target.name} and re-executing with "
            "LD_LIBRARY_PATH so onnxruntime can load cuDNN")
    os.execve(sys.executable, [sys.executable, *sys.argv],
              {**os.environ, "LD_LIBRARY_PATH": search, "JLR_CUDNN_REEXEC": "1"})


def select_providers(want_gpu: bool) -> tuple[list[str], list[str]]:
    """Choose onnxruntime execution providers, always with CPU as a fallback.

    Parameters
    ----------
    want_gpu : bool
        Prefer a GPU provider (CUDA, then ROCm) when one is registered.

    Returns
    -------
    tuple[list[str], list[str]]
        The chosen provider list, and everything the installed onnxruntime offers, for
        logging: ``get_available_providers()`` is how a silent GPU-load failure becomes
        visible, since onnxruntime drops providers it cannot load without erroring.

    Notes
    -----
    CPU is ALWAYS appended. The runtime weights are int8, and an int8 operator the CUDA
    provider has no kernel for is placed on CPU instead of failing the session. That is
    also why a GPU buys little on these weights: onnxruntime reports "336 Memcpy nodes
    are added to the graph" and the batch crosses the bus once per partition boundary.
    """
    available = list(ort.get_available_providers())
    if want_gpu:
        for gpu in ("CUDAExecutionProvider", "ROCMExecutionProvider"):
            if gpu in available:
                return [gpu, "CPUExecutionProvider"], available
    return ["CPUExecutionProvider"], available


def _resolve_intra_threads(n: int) -> int:
    """Resolve the onnxruntime intra-op thread count from ``n``.

    A positive ``n`` is a literal thread count. ``n <= 0`` is interpreted relative to
    the available CPUs, joblib-style: ``-1`` = all cores, ``-2`` = all but one (formally
    ``cpu + 1 + n``), ``0`` = all cores. The result is floored at 1 and capped at the CPU
    count, so it is always usable. On Linux the count is the container's CPU affinity
    (``sched_getaffinity``), so a cpuset-limited container does not over-subscribe."""
    n = int(n)
    if n >= 1:
        return n
    try:
        cpu = max(1, len(os.sched_getaffinity(0)))  # respects a cgroup cpuset
    except AttributeError:  # not Linux (e.g. a macOS dev box)
        cpu = max(1, os.cpu_count() or 1)
    return min(cpu, max(1, cpu + 1 + n))


class Encoder:
    """A warm ONNX feature-extraction encoder: load once, embed many.

    ``session.run`` is thread-safe, so ONE instance is shared across the request
    threads (queries) and the background worker (page passages) in embed-service.py.
    """

    def __init__(
        self,
        model_dir: str | Path = DEFAULT_MODEL_DIR,
        model_name: str = RUNTIME_MODEL,
        intra_threads: int = 4,
        query_cache: int = 256,
        query_ttl: float = 60.0,
        providers: list[str] | None = None,
        passage_batch_size: int = 32,
        out_dim: int | None = None,
        weights: str | None = None,
        max_tokens: int = 0,
    ) -> None:
        model_dir = Path(model_dir)
        prof = _profile(model_name)
        # ``weights`` overrides which ONNX file under <model_dir>/onnx/ is loaded, leaving
        # every other field of the profile (pooling, prefixes, MRL width) alone. The
        # runtime service never passes it: the VPS embeds queries with the profile's int8
        # weights, and a passage vector has to come from the same recipe. The OFFLINE
        # bakes do, to reach the fp32 model.onnx, which is the only way a GPU helps here
        # (arctic published an fp16 graph for this and jina does not). The
        # int8 graph has no CUDA kernels for its quantised operators, so onnxruntime
        # splits it and reports "336 Memcpy nodes are added to the graph": measured on
        # this machine over 256 real sections, an RTX 3090 Ti does 4.2 sections/s against
        # 3.8 on six CPU cores, i.e. nothing. fp16 is a real GPU artefact and the sibling
        # justelesrecos measured its bake at roughly 50x the int8 CPU rate. The catch it
        # also measured: fp16-baked passages reorder about 10% of a top 10 against
        # int8-baked ones, while changing retrieval by nothing 117 queries could resolve.
        onnx_path = model_dir / "onnx" / (weights or prof["onnx"])
        tok_path = model_dir / "tokenizer.json"
        if not onnx_path.is_file() or not tok_path.is_file():
            raise FileNotFoundError(
                f"model not found under {model_dir} (run ./scripts/download-model.sh): "
                f"need onnx/{weights or prof['onnx']} + tokenizer.json"
            )
        opts = ort.SessionOptions()
        # intra_threads may be negative (relative to the CPU count); resolve it here so
        # every caller (embed-service.py, embed-rcp.py) shares one meaning. See
        # _resolve_intra_threads: -1 = all cores, -2 = all but one, positive = literal.
        opts.intra_op_num_threads = _resolve_intra_threads(intra_threads)
        opts.inter_op_num_threads = 1
        # ``providers`` defaults to CPU-only: that is the runtime container's posture (the
        # VPS has no GPU and installs the CPU-only ``onnxruntime``). The offline pre-bake
        # (embed-rcp.py) passes a GPU-preferring list on a machine that has one, always
        # with CPU appended so an unsupported int8 op / a missing GPU degrades to CPU
        # instead of failing. onnxruntime silently drops providers it can't load.
        self.session = ort.InferenceSession(
            str(onnx_path), opts, providers=providers or ["CPUExecutionProvider"]
        )
        # Batch size for PASSAGE encodes (queries are always a batch of 1). 32 suits CPU;
        # a GPU is better fed with a larger batch (embed-rcp.py raises it). Kept as an
        # attribute so build.embed_page_to_vec -> encode_passages needs no new argument.
        self.passage_batch_size = max(1, int(passage_batch_size))
        self._input_names = {i.name for i in self.session.get_inputs()}
        # The ONNX may expose several outputs (arctic-l-v2.0 exposes token_embeddings
        # [B,L,H] AND a pre-pooled sentence_embedding [B,H]); we pool in-code, so target
        # the 3-D token-embeddings output (fallback: the first output).
        outs = self.session.get_outputs()
        self._token_output = outs[0].name
        for o in outs:
            if o.shape is not None and len(o.shape) == 3:
                self._token_output = o.name
                break
        self.tokenizer = Tokenizer.from_file(str(tok_path))
        # tokenizer.json ships with truncation at 512 tokens, and that ceiling is
        # applied inside encode_batch, BEFORE the per-call `max_len` slice, so a caller
        # asking for 1024 silently got 512. It never bit a passage (the longest chunk
        # this corpus produces is 360 tokens) but it cut 79% of the whole-page rows the
        # sibling bakes for its page-level signal, at half the length it believed it was
        # embedding. `max_tokens` raises the ceiling once, here, rather than per call:
        # the tokenizer is shared by every concurrent encode in the service, so mutating
        # it inside encode() would be a race. 0 (the default, and what the service
        # passes) leaves tokenizer.json's own setting alone.
        if max_tokens:
            self.tokenizer.enable_truncation(max_length=max_tokens)
        self.max_tokens = max_tokens
        self.model_name = model_name
        self.pooling = prof["pooling"]
        self.query_prefix, self.passage_prefix = prof["query"], prof["passage"]
        # Matryoshka (MRL) truncation width. ``out_dim`` (wired from EMBED_OUT_DIM by the
        # services) OVERRIDES the model profile's default when given: a positive int
        # truncates to that many dims, 0 keeps the full model width, and None (the
        # default) uses the profile's out_dim (arctic-embed-l-v2.0 -> 1024). Truncating
        # below the model's native width is only meaningful for an MRL-trained model
        # (arctic v2.0 is), so keep EMBED_OUT_DIM aligned with RUNTIME_MODEL. Changing it
        # re-embeds the whole catalog (the dim is baked into each .vec.json and gated on;
        # see build.read_vec_meta / embed_page_to_vec).
        if out_dim is None:
            self._out_dim = prof["out_dim"]
        else:
            self._out_dim = out_dim if out_dim > 0 else None
        # The model's NATIVE width, before any MRL truncation. Read separately from
        # self.dim because a per-request width (encode(width=...)) has to be
        # validated against what the model can actually produce, and self.dim is
        # the SERVED width, which is usually narrower. 0 means "not known yet",
        # and the first full-width encode fills it in.
        self.full_dim = 0
        cfg = model_dir / "config.json"
        if cfg.is_file():
            try:
                self.full_dim = int(json.loads(cfg.read_text())["hidden_size"])
            except Exception:
                pass
        # Served vector width: the MRL truncation length if set (arctic -> 256), else the
        # model's hidden size from config.json (fallback 384). Re-confirmed on 1st encode.
        self.dim = self._out_dim or self.full_dim or 384
        # Bounded, TIME-LIMITED LRU of query-HASH -> (vector, expiry), so repeated/edited
        # queries (common as the reader types) recompute nothing. Keyed by a hash of the
        # query text, NOT the text itself, so no plaintext query is ever retained in the
        # process (only a hash -> lossy vector). The query_ttl (default 60s) bounds HOW
        # LONG even that hash+vector lingers: a hash is a verification oracle (given a
        # guessed query one can hash it and test membership), so expiring entries shortly
        # after use keeps the "query dropped right after encoding" promise strong instead
        # of letting entries sit until LRU eviction. Purged lazily on access AND swept by
        # the caller's periodic loop so idle entries do not persist. Encoder-only concern;
        # passages aren't cached (each is embedded once and persisted to its .vec.json).
        self._q_cache: "OrderedDict[bytes, tuple[np.ndarray, float]]" = OrderedDict()
        self._q_cache_max = max(0, int(query_cache))
        self._q_ttl = max(0.0, float(query_ttl))  # 0 => no expiry

    # -- core --------------------------------------------------------------
    def encode(
        self, texts: list[str], prefix: str = "", batch_size: int = 32, max_len: int = 192,
        width: int | None = None
    ) -> np.ndarray:
        """Embed texts -> float32 (N, dim): pooled per the model (CLS for arctic, mean for
        e5), optionally MRL-truncated (arctic -> 256), then L2-normalised (so cosine == dot
        product). ``prefix`` is prepended to each text (pass ``self.passage_prefix`` for
        documents, ``self.query_prefix`` for queries). Empty input -> (0, dim).

        ``width`` overrides the MRL truncation FOR THIS CALL ONLY: None (default) uses
        the configured out_dim, 0 keeps the model's full width, a positive int truncates
        to that many dims. It exists so one encoder can serve two indexes baked at
        different widths, which is what /api/sem/embed's per-request ``dim`` needs. A
        call that passes ``width`` explicitly does NOT update ``self.dim``: that attribute
        is the SERVED passage width and the staleness gate reads it, so letting a query
        at another width move it would silently invalidate the whole catalog."""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        # Resolve the MRL width ONCE, here, and never inside the batch loop. The
        # loop needs a local for the padded token length, and when that local was
        # also called `width` it shadowed this parameter: `truncate` became the
        # number of TOKENS in the batch, so every vector came back 6 or 13 dims
        # wide and the service reported those as its output width. Hence
        # `seq_len` below, and hence this line living outside the loop.
        truncate = self._out_dim if width is None else (width or None)
        out: list[np.ndarray] = []
        for start in range(0, len(texts), batch_size):
            batch = [prefix + t for t in texts[start : start + batch_size]]
            feed = batch_feed(self.tokenizer, batch, max_len=max_len,
                              token_type_ids="token_type_ids" in self._input_names)
            attention = feed["attention_mask"]
            hidden = self.session.run([self._token_output], feed)[0]  # (B, L, H)
            if self.pooling == "cls":
                vecs = hidden[:, 0, :]  # first token (<s> = CLS): arctic-embed v2.0
            elif self.pooling == "last":
                # The last UNMASKED token, found as the last 1 in the attention
                # mask rather than as ``mask.sum() - 1``. The two agree only when
                # padding is on the right, and a Qwen tokenizer is as likely to pad
                # on the left; reading the mask backwards is correct either way and
                # costs one argmax per batch.
                last = attention.shape[1] - 1 - np.argmax(attention[:, ::-1], axis=1)
                vecs = hidden[np.arange(hidden.shape[0]), last, :]
            else:
                mask = attention[:, :, None].astype(np.float32)
                summed = (hidden * mask).sum(axis=1)
                counts = np.clip(mask.sum(axis=1), 1e-9, None)
                vecs = summed / counts  # mean-pool over the attention mask: e5
            if truncate and vecs.shape[1] > truncate:
                vecs = vecs[:, :truncate]  # MRL: truncate BEFORE normalising
            norms = np.clip(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12, None)
            out.append((vecs / norms).astype(np.float32))
        result = np.vstack(out)
        if width is None:
            self.dim = int(result.shape[1])
        elif not width:
            # A full-width pass is the cheapest place to learn the native size,
            # and it costs nothing: truncation happens after the forward pass.
            self.full_dim = int(result.shape[1])
        return result

    def encode_passages(self, texts: list[str]) -> np.ndarray:
        """Embed document chunks (adds the passage prefix), at ``passage_batch_size``."""
        return self.encode(texts, prefix=self.passage_prefix,
                           batch_size=self.passage_batch_size)

    def purge_expired_queries(self, now: float | None = None) -> int:
        """Drop every cached query entry past its TTL and return how many were removed.
        Called lazily by encode_query and by the service's periodic loop so idle
        entries do not linger past query_ttl even when no new query arrives. No-op when
        query_ttl is 0 (expiry disabled). Not thread-locked: the OrderedDict ops are
        atomic under the GIL and a racing miss just re-encodes, which is harmless."""
        if not self._q_ttl or not self._q_cache:
            return 0
        if now is None:
            now = time.monotonic()
        dead = [k for k, (_, exp) in self._q_cache.items() if now >= exp]
        for k in dead:
            self._q_cache.pop(k, None)
        return len(dead)

    def encode_query(self, query: str, dim: int | None = None) -> np.ndarray:
        """Embed ONE query (adds the query prefix), memoised in the TTL-bounded LRU.

        Returns a 1-D float32 vector of length ``dim``, or of ``self.dim`` when ``dim``
        is None. The cache is keyed by a hash of the query so the plaintext text stays a
        local that is dropped when this returns; only a hash -> vector pair lives in the
        LRU (never the query itself), and only until it expires (query_ttl).

        THE CACHE HOLDS THE FULL-WIDTH VECTOR and this truncates on the way out, which
        is why two sites wanting two different widths share one cache entry and one
        forward pass. That is exact, not an approximation: truncating an L2-normalised
        vector and renormalising gives the same result as truncating the raw vector and
        normalising, because the first normalisation is a positive scalar. It is the
        same identity that lets embed.py store one full-width matrix and sweep every
        width for free (see evaluate.py's ``narrow``).
        """
        text = query.strip()
        key = hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()
        now = time.monotonic()

        def fit(vec: np.ndarray) -> np.ndarray:
            """Truncate the cached full-width vector to the requested width."""
            want = self.dim if dim is None else (dim or vec.shape[0])
            if want >= vec.shape[0]:
                return vec
            cut = vec[:want]
            return (cut / max(float(np.linalg.norm(cut)), 1e-12)).astype(np.float32)

        cached = self._q_cache.get(key)
        if cached is not None:
            vec, exp = cached
            if not self._q_ttl or now < exp:
                self._q_cache.move_to_end(key)
                return fit(vec)
            self._q_cache.pop(key, None)  # expired: forget this query's derived data
        # width=0: encode at the model's native width regardless of out_dim, so the
        # cached vector can serve any requested width. Same forward pass, same cost;
        # MRL truncation happens after it.
        vec = self.encode([text], prefix=self.query_prefix, width=0)[0]
        if self._q_cache_max:
            self.purge_expired_queries(now)  # cheap sweep (<= cache_max entries)
            self._q_cache[key] = (vec, now + self._q_ttl if self._q_ttl else float("inf"))
            while len(self._q_cache) > self._q_cache_max:
                self._q_cache.popitem(last=False)
        return fit(vec)


if __name__ == "__main__":
    # Tiny self-test / latency probe (needs ./scripts/download-model.sh's model).
    import time

    enc = Encoder(intra_threads=1)
    q = "query: puis-je le prendre pendant la grossesse ?"
    t0 = time.perf_counter()
    v = enc.encode_query("puis-je le prendre pendant la grossesse ?")
    dt = (time.perf_counter() - t0) * 1000
    print(f"model={enc.model_name} dim={enc.dim} |v|={np.linalg.norm(v):.4f} "
          f"first-query={dt:.1f} ms")

    # Per-request width, checked here because the first version of it was wrong
    # in a way nothing else would have caught: the `width` parameter was shadowed
    # by a loop local holding the padded TOKEN COUNT, so every vector came back 6
    # or 13 dims wide and /api/sem/embed reported that as its output width. The
    # assertion is on the width, so any future shadowing fails loudly here.
    assert enc.full_dim, "native width should be known after a query encode"
    for want in (enc.full_dim, enc.full_dim // 2, enc.dim):
        got = enc.encode_query(q, dim=want)
        assert len(got) == want, f"asked for {want} dims, got {len(got)}"
        assert abs(float(np.linalg.norm(got)) - 1.0) < 1e-4, "not unit length"
    # MRL identity: truncating the cached full-width vector and renormalising is
    # the same vector as a fresh narrow encode, which is what lets ONE cached
    # query vector serve every requested width.
    narrow = Encoder(intra_threads=1, out_dim=enc.dim, query_cache=0)
    direct = narrow.encode([q], prefix=narrow.query_prefix, width=enc.dim)[0]
    cos = float(enc.encode_query(q, dim=enc.dim) @ direct)
    assert cos > 0.9999, f"MRL identity broken: cos={cos}"
    print(f"per-request width ok (native={enc.full_dim}, default={enc.dim}), "
          f"MRL identity cos={cos:.6f}")
