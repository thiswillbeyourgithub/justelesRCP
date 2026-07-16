#!/usr/bin/env bash
# Fetch the self-hosted assets for per-drug client-side semantic search into
# ./vendor (transformers.js + onnxruntime-web wasm) and ./models (the ONNX encoder).
# Both dirs are gitignored and large (~120 MB, like ./data); build.py mirrors them
# into ./dist so Caddy serves them SAME-ORIGIN (nothing is fetched from a CDN or the
# HF hub at serve time, keeping the site 100% static and the strict CSP intact).
#
# OPTIONAL: skip it and the "Rechercher dans ce RCP" box degrades to a graceful
# "pas encore disponible" note. Re-run any time; it skips files already present.
# Versions are pinned below; a bump here is a new build (the referencing code and
# the Cache-Control immutable policy in docker/Caddyfile assume content-versioning).
set -euo pipefail
cd "$(dirname "$0")"

# @huggingface/transformers ships the browser JS (transformers.min.js) but NOT the
# heavy wasm binaries; those live in its onnxruntime-web dependency. So we pull two
# npm tarballs: transformers for the JS entry, onnxruntime-web for the wasm runtime.
# ORT_VERSION MUST match the onnxruntime-web version transformers pins (the wasm has
# to match the JS glue bundled in transformers.min.js), so bump both together. Keep
# the model in sync with RUNTIME_MODEL (src/rcp-semsearch.js) + EMBED_MODEL
# (embed-rcp.py): the baked passage vectors and the runtime query vector must come
# from the same weights.
TRANSFORMERS_VERSION="4.2.0"
ORT_VERSION="1.26.0-dev.20260416-b7804b056c"   # == @huggingface/transformers@4.2.0's onnxruntime-web pin
MODEL_REPO="Xenova/multilingual-e5-small"
HF_BASE="https://huggingface.co/${MODEL_REPO}/resolve/main"
TRANSFORMERS_TARBALL="https://registry.npmjs.org/@huggingface/transformers/-/transformers-${TRANSFORMERS_VERSION}.tgz"
ORT_TARBALL="https://registry.npmjs.org/onnxruntime-web/-/onnxruntime-web-${ORT_VERSION}.tgz"

mkdir -p vendor/ort "models/${MODEL_REPO}/onnx"

# The wasm MUST be served from our own origin: rcp-semsearch.js points ort's
# wasmPaths at /vendor/ort/ and never falls back to a CDN, so an incomplete vendor/
# breaks the offline guarantee. Each file is checked INDIVIDUALLY (not gated behind
# one another) so a re-run after a partial download self-heals.

# 1a. transformers.js browser ESM entry (imported by rcp-semsearch.js), from the
#     @huggingface/transformers tarball. Small (~0.5 MB); the wasm is NOT in here.
if [ ! -f vendor/transformers.min.js ]; then
  echo "Downloading @huggingface/transformers@${TRANSFORMERS_VERSION} (npm)..."
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  wget -O "$tmp/tf.tgz" "$TRANSFORMERS_TARBALL"
  tar -xzf "$tmp/tf.tgz" -C "$tmp"   # -> $tmp/package/dist/...
  cp -f "$tmp/package/dist/transformers.min.js" vendor/
  rm -rf "$tmp"; trap - EXIT
  echo "  vendor/transformers.min.js ready."
fi

# 1b. The onnxruntime-web wasm runtime that transformers.min.js loads at run time:
#     the single-thread CPU build plus its asyncify sibling (belt-and-braces for the
#     proxy path; rcp-semsearch.js sets numThreads=1 + proxy=false). ~36 MB total.
#     These are the exact basenames transformers.min.js references from wasmPaths.
ORT_FILES="ort-wasm-simd-threaded.wasm ort-wasm-simd-threaded.mjs \
           ort-wasm-simd-threaded.asyncify.wasm ort-wasm-simd-threaded.asyncify.mjs"
ort_need=0
for f in $ORT_FILES; do [ -f "vendor/ort/$f" ] || ort_need=1; done
if [ "$ort_need" = 1 ]; then
  echo "Downloading onnxruntime-web@${ORT_VERSION} wasm (npm)..."
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  wget -O "$tmp/ort.tgz" "$ORT_TARBALL"
  tar -xzf "$tmp/ort.tgz" -C "$tmp"   # -> $tmp/package/dist/...
  for f in $ORT_FILES; do
    cp -f "$tmp/package/dist/$f" vendor/ort/
  done
  rm -rf "$tmp"; trap - EXIT
  echo "  vendor/ort/*.wasm ready."
fi

# 2. The ONNX encoder + tokenizer from the Hugging Face hub. The int8-quantised
#    model_quantized.onnx is the ~120 MB payload; the JSON files are tiny. These
#    load in the browser via transformers.js with { quantized: true }.
for f in config.json tokenizer.json tokenizer_config.json special_tokens_map.json; do
  if [ ! -f "models/${MODEL_REPO}/$f" ]; then
    echo "Downloading ${MODEL_REPO}/$f ..."
    wget -O "models/${MODEL_REPO}/$f" "${HF_BASE}/$f"
  fi
done
if [ ! -f "models/${MODEL_REPO}/onnx/model_quantized.onnx" ]; then
  echo "Downloading ${MODEL_REPO}/onnx/model_quantized.onnx (~120 MB, one time)..."
  wget -O "models/${MODEL_REPO}/onnx/model_quantized.onnx" "${HF_BASE}/onnx/model_quantized.onnx"
fi

echo "Done. vendor/ + models/ ready."
echo "Next: uv run embed-rcp.py --limit 60   (offline build-time vectors), then"
echo "      uv run build.py                   (bakes .vec.json + mirrors vendor/models into dist/)"
