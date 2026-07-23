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
``Snowflake/snowflake-arctic-embed-l-v2.0`` ONNX weights (CLS-pooled, L2-normalised,
MRL-truncated to 256 dims), driven by a per-model recipe (``_profile``) so the query
and passage sides always share one backend + one set of weights.

Pure + import-safe (``__main__`` guard); no filesystem writes, no network.
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
RUNTIME_MODEL = "Snowflake/snowflake-arctic-embed-l-v2.0"
# models/ lives at the repo root (this script is in src/), and is mounted at
# ``/app/models`` in the embed container (HERE = ``/app/src`` there); parent resolves
# both. EMBED_MODEL_DIR overrides it in the container anyway.
DEFAULT_MODEL_DIR = HERE.parent / "models" / RUNTIME_MODEL


def _profile(model_name: str) -> dict:
    """Per-model runtime recipe, keyed by a substring of the name so a swap only touches
    RUNTIME_MODEL (+ the matching scripts/download-model.sh fetch). Fields:
      onnx    : the int8 ONNX file under <model_dir>/onnx/ to load
      pooling : "cls" (first token; arctic-embed v2.0 / XLM-R lineage) or "mean" (e5)
      query   : prefix prepended to a QUERY before tokenising
      passage : prefix prepended to a DOCUMENT/passage (arctic: NONE; e5: "passage: ")
      out_dim : Matryoshka (MRL) truncation length, or None to keep the full width

    arctic-embed-l-v2.0: CLS-pool -> L2-normalise, query-only "query: " prefix, MRL to
    256 (truncate THEN normalise once; the ST pipeline's pre-truncation normalise is a
    mathematical no-op). Verified against the repo's 1_Pooling/config.json +
    config_sentence_transformers.json + the ONNX graph (inputs input_ids/attention_mask
    only, output token_embeddings [B,L,1024])."""
    n = model_name.lower()
    if "arctic-embed" in n:
        return {"onnx": "model_int8.onnx", "pooling": "cls",
                "query": "query: ", "passage": "", "out_dim": 256}
    if "e5" in n:
        return {"onnx": "model_quantized.onnx", "pooling": "mean",
                "query": "query: ", "passage": "passage: ", "out_dim": None}
    # Unknown model: safe defaults (mean pool, no prefixes, full width, common ONNX name).
    return {"onnx": "model_quantized.onnx", "pooling": "mean",
            "query": "", "passage": "", "out_dim": None}


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
    ) -> None:
        model_dir = Path(model_dir)
        prof = _profile(model_name)
        onnx_path = model_dir / "onnx" / prof["onnx"]
        tok_path = model_dir / "tokenizer.json"
        if not onnx_path.is_file() or not tok_path.is_file():
            raise FileNotFoundError(
                f"model not found under {model_dir} (run ./scripts/download-model.sh): "
                f"need onnx/{prof['onnx']} + tokenizer.json"
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
        self.model_name = model_name
        self.pooling = prof["pooling"]
        self.query_prefix, self.passage_prefix = prof["query"], prof["passage"]
        # Matryoshka (MRL) truncation width. ``out_dim`` (wired from EMBED_OUT_DIM by the
        # services) OVERRIDES the model profile's default when given: a positive int
        # truncates to that many dims, 0 keeps the full model width, and None (the
        # default) uses the profile's out_dim (arctic-embed-l-v2.0 -> 256). Truncating
        # below the model's native width is only meaningful for an MRL-trained model
        # (arctic v2.0 is), so keep EMBED_OUT_DIM aligned with RUNTIME_MODEL. Changing it
        # re-embeds the whole catalog (the dim is baked into each .vec.json and gated on;
        # see build.read_vec_meta / embed_page_to_vec).
        if out_dim is None:
            self._out_dim = prof["out_dim"]
        else:
            self._out_dim = out_dim if out_dim > 0 else None
        # Served vector width: the MRL truncation length if set (arctic -> 256), else the
        # model's hidden size from config.json (fallback 384). Re-confirmed on 1st encode.
        self.dim = self._out_dim or 384
        if not self._out_dim:
            cfg = model_dir / "config.json"
            if cfg.is_file():
                try:
                    self.dim = int(json.loads(cfg.read_text())["hidden_size"])
                except Exception:
                    pass
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
        self, texts: list[str], prefix: str = "", batch_size: int = 32, max_len: int = 192
    ) -> np.ndarray:
        """Embed texts -> float32 (N, dim): pooled per the model (CLS for arctic, mean for
        e5), optionally MRL-truncated (arctic -> 256), then L2-normalised (so cosine == dot
        product). ``prefix`` is prepended to each text (pass ``self.passage_prefix`` for
        documents, ``self.query_prefix`` for queries). Empty input -> (0, dim)."""
        if not texts:
            return np.zeros((0, self.dim), dtype=np.float32)
        out: list[np.ndarray] = []
        for start in range(0, len(texts), batch_size):
            batch = [prefix + t for t in texts[start : start + batch_size]]
            encs = self.tokenizer.encode_batch(batch)
            ids_list = [e.ids[:max_len] for e in encs]
            width = max((len(x) for x in ids_list), default=1) or 1
            input_ids = np.zeros((len(batch), width), dtype=np.int64)
            attention = np.zeros((len(batch), width), dtype=np.int64)
            for row, ids in enumerate(ids_list):
                input_ids[row, : len(ids)] = ids
                attention[row, : len(ids)] = 1
            feed = {"input_ids": input_ids, "attention_mask": attention}
            if "token_type_ids" in self._input_names:
                feed["token_type_ids"] = np.zeros_like(input_ids)
            hidden = self.session.run([self._token_output], feed)[0]  # (B, L, H)
            if self.pooling == "cls":
                vecs = hidden[:, 0, :]  # first token (<s> = CLS): arctic-embed v2.0
            else:
                mask = attention[:, :, None].astype(np.float32)
                summed = (hidden * mask).sum(axis=1)
                counts = np.clip(mask.sum(axis=1), 1e-9, None)
                vecs = summed / counts  # mean-pool over the attention mask: e5
            if self._out_dim and vecs.shape[1] > self._out_dim:
                vecs = vecs[:, : self._out_dim]  # MRL: truncate BEFORE normalising
            norms = np.clip(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12, None)
            out.append((vecs / norms).astype(np.float32))
        result = np.vstack(out)
        self.dim = int(result.shape[1])
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

    def encode_query(self, query: str) -> np.ndarray:
        """Embed ONE query (adds the query prefix), memoised in the TTL-bounded LRU.
        Returns a 1-D float32 vector of length ``dim``. The cache is keyed by a hash of
        the query so the plaintext text stays a local that is dropped when this returns;
        only a hash -> vector pair lives in the LRU (never the query itself), and only
        until it expires (query_ttl)."""
        text = query.strip()
        key = hashlib.blake2b(text.encode("utf-8"), digest_size=16).digest()
        now = time.monotonic()
        cached = self._q_cache.get(key)
        if cached is not None:
            vec, exp = cached
            if not self._q_ttl or now < exp:
                self._q_cache.move_to_end(key)
                return vec
            self._q_cache.pop(key, None)  # expired: forget this query's derived data
        vec = self.encode([text], prefix=self.query_prefix)[0]
        if self._q_cache_max:
            self.purge_expired_queries(now)  # cheap sweep (<= cache_max entries)
            self._q_cache[key] = (vec, now + self._q_ttl if self._q_ttl else float("inf"))
            while len(self._q_cache) > self._q_cache_max:
                self._q_cache.popitem(last=False)
        return vec


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
