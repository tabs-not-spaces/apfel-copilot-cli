#!/usr/bin/env bash
#
# copilot-apfel.sh
# Run GitHub Copilot CLI against the local apfel server (Apple FoundationModels)
# using Copilot CLI's built-in BYOK (Bring Your Own Key) provider support.
#
# apfel exposes Apple's on-device LLM as an OpenAI-compatible server:
#   apfel --serve   ->   http://localhost:11434/v1
#
# Copilot CLI's COPILOT_PROVIDER_* env vars point the CLI at any
# OpenAI-compatible endpoint instead of GitHub's model routing.
#
# NOTE / KNOWN LIMITATION (4096-token context window):
#   Apple's FoundationModels model has a HARD 4096-token context window.
#   A raw Copilot CLI request is ~107k tokens (226 tool schemas ~103k +
#   system prompt ~6.2k), so direct BYOK fails:
#     "400 Input exceeds the 4096-token context window."
#
#   This launcher routes Copilot CLI through apfel_proxy.py, which strips
#   tool schemas, truncates the system prompt, and rolls history into local
#   files (~/.apfel-copilot/) so each request fits 4096. Because tools are
#   stripped, this is a working CHAT against the on-device model, NOT the
#   full file-editing / shell agent. That is the unavoidable price of 4096.

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APFEL_URL="${APFEL_URL:-http://localhost:11434/v1}"
PROXY_PORT="${APFEL_PROXY_PORT:-8898}"
PROXY_URL="http://localhost:${PROXY_PORT}/v1"
APFEL_MODEL="apple-foundationmodel"

# Start apfel server if not already running.
if ! curl -s -m 3 "${APFEL_URL}/models" >/dev/null 2>&1; then
  echo "apfel server not up; starting 'apfel --serve'..." >&2
  apfel --serve >/tmp/apfel-serve.log 2>&1 &
  for _ in $(seq 1 15); do
    sleep 1
    curl -s -m 3 "${APFEL_URL}/models" >/dev/null 2>&1 && break
  done
fi

# Start the context-fitting proxy if not already running.
if ! curl -s -m 3 "${PROXY_URL}/models" >/dev/null 2>&1; then
  echo "starting apfel_proxy.py on :${PROXY_PORT}..." >&2
  APFEL_PROXY_PORT="${PROXY_PORT}" python3 "${HERE}/apfel_proxy.py" >/tmp/apfel-proxy.log 2>&1 &
  for _ in $(seq 1 10); do
    sleep 1
    curl -s -m 3 "${PROXY_URL}/models" >/dev/null 2>&1 && break
  done
fi

export COPILOT_PROVIDER_BASE_URL="${PROXY_URL}"
export COPILOT_PROVIDER_TYPE="openai"          # apfel = OpenAI-compatible
export COPILOT_PROVIDER_API_KEY="apfel-local"  # apfel needs none; dummy keeps CLI happy
export COPILOT_PROVIDER_WIRE_MODEL="${APFEL_MODEL}"   # name sent on the wire
export COPILOT_PROVIDER_MODEL_ID="${APFEL_MODEL}"     # well-known id for limits/agent cfg
export COPILOT_MODEL="${APFEL_MODEL}"
export COPILOT_PROVIDER_MAX_PROMPT_TOKENS="3500"
export COPILOT_PROVIDER_MAX_OUTPUT_TOKENS="512"
export COPILOT_OFFLINE="1"                      # skip GitHub auth/telemetry/web/auto-update

exec copilot "$@"
