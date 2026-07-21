#!/usr/bin/env bash
# Fetch the ONNX sentence encoder for per-drug semantic search into ./models. The
# model now runs SERVER-SIDE (embed-service.py keeps it warm and embeds queries +
# page passages); it is NO LONGER shipped to the browser, so this script only pulls
# the model + tokenizer (no transformers.js / onnxruntime-web browser wasm anymore).
# ./models is gitignored and ~570 MB (the arctic int8 weights; like ./data); the embed
# container mounts it read-only. Nothing is served to browsers from here, so the strict
# CSP needs no 'wasm-unsafe-eval' relaxation.
#
# OPTIONAL: skip it and the "Rechercher dans ce RCP" box degrades to a graceful
# "indisponible" note. Re-run any time; it skips files already present. Keep
# MODEL_REPO in sync with onnx_embed.RUNTIME_MODEL (embed-service.py + embed-rcp.py):
# the baked passage vectors and the runtime query vector must come from the same
# weights.
set -euo pipefail
cd "$(dirname "$0")/.."  # anchor at the repo root (this script lives in scripts/)

# Keep MODEL_REPO + ONNX_FILE in sync with onnx_embed._profile (the file it loads under
# onnx/): arctic-embed-l-v2.0 -> onnx/model_int8.onnx.
MODEL_REPO="Snowflake/snowflake-arctic-embed-l-v2.0"
ONNX_FILE="model_int8.onnx"
HF_BASE="https://huggingface.co/${MODEL_REPO}/resolve/main"

mkdir -p "models/${MODEL_REPO}/onnx"

# The int8-quantised ${ONNX_FILE} is the ~570 MB payload; config.json + tokenizer.json
# are tiny (the fast tokenizer.json is self-contained, so no separate tokenizer_config /
# special_tokens_map is needed). onnx_embed.Encoder loads onnx/${ONNX_FILE} +
# tokenizer.json directly (onnxruntime + tokenizers, no torch). Each file is checked
# INDIVIDUALLY so a re-run after a partial download self-heals.
for f in config.json tokenizer.json; do
  if [ ! -f "models/${MODEL_REPO}/$f" ]; then
    echo "Downloading ${MODEL_REPO}/$f ..."
    wget -O "models/${MODEL_REPO}/$f" "${HF_BASE}/$f"
  fi
done
if [ ! -f "models/${MODEL_REPO}/onnx/${ONNX_FILE}" ]; then
  echo "Downloading ${MODEL_REPO}/onnx/${ONNX_FILE} (~570 MB, one time)..."
  wget -O "models/${MODEL_REPO}/onnx/${ONNX_FILE}" "${HF_BASE}/onnx/${ONNX_FILE}"
fi

echo "Done. models/${MODEL_REPO} ready (mounted read-only into the embed container)."
echo "Next: docker compose -f docker/docker-compose.yml up -d --build   (starts the embed service), or"
echo "      uv run src/embed-rcp.py --limit 60   (OPTIONAL: pre-bake vectors offline to warm the backlog)"
