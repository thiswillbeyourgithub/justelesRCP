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
and it lets the hardened, read-only runtime container stay tiny. It runs the same
int8 ``Xenova/multilingual-e5-small`` ONNX weights the browser used, so query and
passage vectors share one backend (better parity than the previous ST-baked-passage
/ ONNX-query split).

Pure + import-safe (``__main__`` guard); no filesystem writes, no network.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections import OrderedDict
from pathlib import Path

import numpy as np
import onnxruntime as ort
from tokenizers import Tokenizer

HERE = Path(__file__).resolve().parent

# The runtime model. MUST match embed-rcp.py's DEFAULT_MODEL family and the vectors
# baked into every .vec.json (a query vector and the passage vectors have to come
# from the same weights). Mounted read-only into the container from ./models by
# download-model.sh; NOT served to browsers anymore.
RUNTIME_MODEL = "Xenova/multilingual-e5-small"
DEFAULT_MODEL_DIR = HERE / "models" / RUNTIME_MODEL

# e5 models require these asymmetric prefixes; the query and passage sides MUST use
# the matching one. Derived from the model name so a non-e5 swap needs no prefix.
_QUERY_PREFIX = "query: "
_PASSAGE_PREFIX = "passage: "


def _prefixes(model_name: str) -> tuple[str, str]:
    """(query_prefix, passage_prefix). e5 wants them; most models use none."""
    if "e5" in model_name.lower():
        return _QUERY_PREFIX, _PASSAGE_PREFIX
    return "", ""


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
    ) -> None:
        model_dir = Path(model_dir)
        onnx_path = model_dir / "onnx" / "model_quantized.onnx"
        tok_path = model_dir / "tokenizer.json"
        if not onnx_path.is_file() or not tok_path.is_file():
            raise FileNotFoundError(
                f"model not found under {model_dir} (run ./download-model.sh): "
                f"need onnx/model_quantized.onnx + tokenizer.json"
            )
        opts = ort.SessionOptions()
        opts.intra_op_num_threads = max(1, int(intra_threads))
        opts.inter_op_num_threads = 1
        self.session = ort.InferenceSession(
            str(onnx_path), opts, providers=["CPUExecutionProvider"]
        )
        self._input_names = {i.name for i in self.session.get_inputs()}
        self.tokenizer = Tokenizer.from_file(str(tok_path))
        self.model_name = model_name
        self.query_prefix, self.passage_prefix = _prefixes(model_name)
        # Hidden size from config.json (fallback 384 = e5-small); confirmed on 1st run.
        self.dim = 384
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
        """Embed texts -> float32 (N, dim), mean-pooled over the attention mask and
        L2-normalised (so cosine == dot product). ``prefix`` is prepended to each
        text (pass ``self.passage_prefix`` for documents, ``self.query_prefix`` for
        queries). Empty input -> (0, dim)."""
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
            hidden = self.session.run(None, feed)[0]  # (B, L, dim)
            mask = attention[:, :, None].astype(np.float32)
            summed = (hidden * mask).sum(axis=1)
            counts = np.clip(mask.sum(axis=1), 1e-9, None)
            vecs = summed / counts
            norms = np.clip(np.linalg.norm(vecs, axis=1, keepdims=True), 1e-12, None)
            out.append((vecs / norms).astype(np.float32))
        result = np.vstack(out)
        self.dim = int(result.shape[1])
        return result

    def encode_passages(self, texts: list[str]) -> np.ndarray:
        """Embed document chunks (adds the passage prefix)."""
        return self.encode(texts, prefix=self.passage_prefix)

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


class CachingEncoder:
    """Wraps an ``Encoder`` and memoises ``encode_passages`` by content hash, so a
    passage embedded once is REUSED for every other page that repeats it verbatim.

    Generics share near-identical RCP text: ~63% of chunks across the catalog are
    cross-drug duplicates, so on a BULK bake this skips ~2/3 of the encodes. The cached
    value IS the wrapped encoder's own float32 output, so a cache HIT yields a
    byte-identical vector to a fresh encode: offline (embed-rcp.py) and online
    (embed-service.py) vectors still agree, and the .vec.json format is unchanged (this
    is a compute optimisation only, no storage/transfer change).

    Intended for the offline batch pre-bake, where the cache is transient and freed when
    the process exits. Deliberately NOT used by the long-lived service: there the
    duplicates are scattered in time, so a persistent cache would cost steady RAM for
    little steady-state benefit. Everything except ``encode_passages`` (query encoding,
    prefixes, ``model_name``, ``dim``, ...) delegates to the wrapped encoder."""

    def __init__(self, encoder: "Encoder") -> None:
        self._enc = encoder
        self._cache: dict[bytes, np.ndarray] = {}
        self.hits = 0
        self.misses = 0

    def __getattr__(self, name: str):  # delegate anything we don't override
        return getattr(self._enc, name)

    def encode_passages(self, texts: list[str]) -> np.ndarray:
        """Same contract as Encoder.encode_passages ((N, dim) float32), but only the
        not-yet-seen texts hit the model; repeats (within this call AND across earlier
        calls) return the memoised row. Order is preserved (rows are reassembled by the
        caller's original index)."""
        if not texts:
            return self._enc.encode_passages(texts)
        keys = [hashlib.blake2b(t.encode("utf-8"), digest_size=16).digest() for t in texts]
        todo_keys: list[bytes] = []
        todo_texts: list[str] = []
        queued: set[bytes] = set()
        for k, t in zip(keys, texts):
            if k not in self._cache and k not in queued:
                queued.add(k)
                todo_keys.append(k)
                todo_texts.append(t)
        if todo_texts:
            fresh = self._enc.encode_passages(todo_texts)
            for j, k in enumerate(todo_keys):
                self._cache[k] = fresh[j]
        self.misses += len(todo_texts)
        self.hits += len(texts) - len(todo_texts)
        return np.vstack([self._cache[k] for k in keys])


if __name__ == "__main__":
    # Tiny self-test / latency probe (needs ./download-model.sh's model).
    import time

    enc = Encoder(intra_threads=1)
    q = "query: puis-je le prendre pendant la grossesse ?"
    t0 = time.perf_counter()
    v = enc.encode_query("puis-je le prendre pendant la grossesse ?")
    dt = (time.perf_counter() - t0) * 1000
    print(f"model={enc.model_name} dim={enc.dim} |v|={np.linalg.norm(v):.4f} "
          f"first-query={dt:.1f} ms")
