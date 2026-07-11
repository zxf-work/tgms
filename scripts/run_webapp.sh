#!/bin/bash
# Serve the interactive guided demo (D-015). Binds 127.0.0.1 by design —
# reach it remotely with:  ssh -N -L 8080:localhost:8080 user@host
# Parameters (env):
#   TGMS_REPO      — tgms checkout (default: repo containing this script)
#   TGMS_STORE     — store dir       (default: stores/collegemsg)
#   TGMS_SUITE     — suite json      (default: stores/suite-collegemsg/suite.json)
#   TGMS_MODEL     — LiteLLM model   (default: openai/Qwen/Qwen2.5-14B-Instruct-AWQ)
#   TGMS_API_BASE  — OpenAI-compatible endpoint (default: http://localhost:8000/v1)
#   TGMS_PORT      — port (default: 8080)
set -e
TGMS_REPO="${TGMS_REPO:-$(cd "$(dirname "$0")/.." && pwd)}"
export PATH="$HOME/.local/bin:$PATH"
cd "$TGMS_REPO"
exec uv run tgms webapp \
  --store "${TGMS_STORE:-stores/collegemsg}" \
  --suite "${TGMS_SUITE:-stores/suite-collegemsg/suite.json}" \
  --model "${TGMS_MODEL:-openai/Qwen/Qwen2.5-14B-Instruct-AWQ}" \
  --api-base "${TGMS_API_BASE:-http://localhost:8000/v1}" \
  --port "${TGMS_PORT:-8080}"
