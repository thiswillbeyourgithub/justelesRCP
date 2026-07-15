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

# @huggingface/transformers bundles onnxruntime-web, so the one npm tarball gives us
# both the browser ESM entry and the wasm runtime. Keep in sync with the RUNTIME_MODEL
# in src/rcp-semsearch.js and EMBED_MODEL in embed-rcp.py (same weights, ONNX build).
TRANSFORMERS_VERSION="4.2.0"
MODEL_REPO="Xenova/multilingual-e5-small"
HF_BASE="https://huggingface.co/${MODEL_REPO}/resolve/main"
NPM_TARBALL="https://registry.npmjs.org/@huggingface/transformers/-/transformers-${TRANSFORMERS_VERSION}.tgz"

mkdir -p vendor/ort "models/${MODEL_REPO}/onnx"

# 1. transformers.js + the onnxruntime-web wasm it needs, from the npm tarball. We
#    take the browser ESM entry (transformers.min.js, imported by rcp-semsearch.js)
#    plus the single-thread CPU wasm runtime and its asyncify sibling (belt-and-braces
#    for the proxy path; rcp-semsearch.js sets numThreads=1 + proxy=false).
if [ ! -f vendor/transformers.min.js ]; then
  echo "Downloading @huggingface/transformers@${TRANSFORMERS_VERSION} (npm)..."
  tmp="$(mktemp -d)"
  trap 'rm -rf "$tmp"' EXIT
  wget -O "$tmp/tf.tgz" "$NPM_TARBALL"
  tar -xzf "$tmp/tf.tgz" -C "$tmp"   # -> $tmp/package/dist/...
  cp "$tmp/package/dist/transformers.min.js" vendor/
  for f in ort-wasm-simd-threaded.wasm ort-wasm-simd-threaded.mjs \
           ort-wasm-simd-threaded.asyncify.wasm ort-wasm-simd-threaded.asyncify.mjs; do
    cp "$tmp/package/dist/$f" vendor/ort/
  done
  rm -rf "$tmp"; trap - EXIT
  echo "  vendor/transformers.min.js + vendor/ort/*.wasm ready."
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
