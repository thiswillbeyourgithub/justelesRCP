#!/usr/bin/env bash
# Fetch the ONNX sentence encoder for per-drug semantic search into ./models. The
# model now runs SERVER-SIDE (embed-service.py keeps it warm and embeds queries +
# page passages); it is NO LONGER shipped to the browser, so this script only pulls
# the model + tokenizer (no transformers.js / onnxruntime-web browser wasm anymore).
# ./models is gitignored and about 3 GB while both graphs are present (the fp32 download
# is 2.38 GB and the int8 graph quantised from it is ~650 MB; the fp32 one can be deleted
# afterwards, see --keep-fp32); the embed container mounts it read-only. Nothing is served to browsers from here, so the strict
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
# onnx/): jina-embeddings-v5-text-small-retrieval -> onnx/model_int8.onnx.
#
# The RETRIEVAL repo, not the base one: jina v5 is task-conditioned, and this is the
# variant with the retrieval adapter merged into the weights, which is what lets a plain
# onnxruntime session run it with no peft and no task argument.
#
# Jina publishes fp32 ONLY (model.onnx plus its 2.38 GB external-data sibling), so unlike
# arctic there is no int8 graph to download: this script fetches the fp32 pair and then
# quantises it locally. The model is CC BY-NC 4.0, which is a licence change from
# arctic-embed's Apache 2.0; both sites are free and ad-free, and DESIGN.md records that
# reading.
MODEL_REPO="jinaai/jina-embeddings-v5-text-small-retrieval"
ONNX_FILE="model_int8.onnx"
FP32_FILE="model.onnx"
HF_BASE="https://huggingface.co/${MODEL_REPO}/resolve/main"

# --keep-fp32 KEEPS onnx/model.onnx + its 2.38 GB _data sibling after quantising, which is
# NOT what the service runs. They exist for one job: an offline GPU bake
# (src/embed-rcp.py --weights model.onnx).
# The int8 graph gains nothing from a GPU, because its quantised operators have no CUDA
# kernels and onnxruntime splits the graph around them ("336 Memcpy nodes are added");
# measured on an RTX 3090 Ti, 4.2 sections/s against 3.8 on six CPU cores. fp16 is a real
# GPU artefact. The trade, measured in the sibling justelesrecos: fp16-baked passages
# reorder about 10% of a top 10 against int8-baked ones and change retrieval by nothing
# 117 queries can resolve, while the queries keep coming from the int8 weights.
WANT_FP16=0
KEEP_FP32=0
for arg in "$@"; do
  case "$arg" in
    --fp16) WANT_FP16=1 ;;
    --keep-fp32) KEEP_FP32=1 ;;
    *) echo "unknown argument: $arg (--fp16 and --keep-fp32 are understood)" >&2; exit 2 ;;
  esac
done

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
# The fp32 pair, then the int8 graph quantised from it. model.onnx is a 1.3 MB stub and
# model.onnx_data holds the 2.38 GB of weights beside it; onnx resolves the pair by the
# relative name recorded in the stub, so the two must land in the same directory.
if [ ! -f "models/${MODEL_REPO}/onnx/${ONNX_FILE}" ]; then
  for f in "${FP32_FILE}" "${FP32_FILE}_data"; do
    if [ ! -f "models/${MODEL_REPO}/onnx/$f" ]; then
      echo "Downloading ${MODEL_REPO}/onnx/$f (the pair is ~2.4 GB, one time)..."
      wget -O "models/${MODEL_REPO}/onnx/$f" "${HF_BASE}/onnx/$f"
    fi
  done
  echo "Quantising to int8 (a few minutes, needs ~10 GB of RAM)..."
  uv run scripts/quantise-model.py \
    --src "models/${MODEL_REPO}/onnx/${FP32_FILE}" \
    --out "models/${MODEL_REPO}/onnx/${ONNX_FILE}"
  if [ "$KEEP_FP32" = "0" ]; then
    echo "Removing the fp32 graph (pass --keep-fp32 to keep it for a GPU bake)."
    rm -f "models/${MODEL_REPO}/onnx/${FP32_FILE}" "models/${MODEL_REPO}/onnx/${FP32_FILE}_data"
  fi
fi

# Jina publishes no fp16 graph either, so the GPU bake reads the fp32 pair. --fp16 keeps
# it rather than downloading anything, which is what --keep-fp32 does; it stays as a
# spelling of the same intent so the sibling's documented invocation does not break.
if [ "$WANT_FP16" = "1" ] && [ ! -f "models/${MODEL_REPO}/onnx/${FP32_FILE}" ]; then
  echo "This model publishes no fp16 graph. Re-run with --keep-fp32 to keep the fp32"
  echo "pair for an offline GPU bake (uv run src/embed-rcp.py --weights model.onnx)."
fi

echo "Done. models/${MODEL_REPO} ready (mounted read-only into the embed container)."
echo "Next: docker compose -f docker/docker-compose.yml up -d --build   (starts the embed service), or"
echo "      uv run src/embed-rcp.py --limit 60   (OPTIONAL: pre-bake vectors offline to warm the backlog)"
