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
# 4096-token context window:
#   Apple's FoundationModels model has a HARD 4096-token context window.
#   A raw Copilot CLI request is ~107k tokens (226 tool schemas ~103k +
#   system prompt ~6.2k), so direct BYOK fails:
#     "400 Input exceeds the 4096-token context window."
#
#   This launcher routes Copilot CLI through one of two proxies, each of which
#   fits every request into 4096 tokens:
#     v2 (default) - apfel_proxy_v2.py: constrained-decoding AGENT bridge. Uses
#                    apfel's json_schema response_format (Apple guided
#                    generation) to drive tool routing + argument filling, then
#                    synthesises clean OpenAI tool_calls -> full file-editing /
#                    shell agent.
#     v1           - apfel_proxy.py: strips tool schemas; working CHAT only.
#                    Legacy fallback for plain conversation.
#
#   Both roll history into local files (~/.apfel-copilot/) so each request fits.
#
# Env overrides:
#   APFEL_URL            apfel server base url   (default http://localhost:11434/v1)
#   APFEL_PROXY_VARIANT  v1 | v2                 (default v2)
#   APFEL_PROXY_PORT     proxy port              (default 8899 for v2, 8898 for v1)
#   APFEL_MAX_TOOLS      v2 tool-RAG cap         (default 8)
#   MAX_PROMPT_TOKENS    window advertised to CLI (default 120000)

set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
APFEL_URL="${APFEL_URL:-http://localhost:11434/v1}"
APFEL_MODEL="apple-foundationmodel"
VARIANT="${APFEL_PROXY_VARIANT:-v2}"

if [[ "${VARIANT}" == "v2" ]]; then
  PROXY_SCRIPT="apfel_proxy_v2.py"
  PORT_ENV="APFEL_PROXY_V2_PORT"
  DEFAULT_PORT=8899
else
  PROXY_SCRIPT="apfel_proxy.py"
  PORT_ENV="APFEL_PROXY_PORT"
  DEFAULT_PORT=8898
fi

PROXY_PORT="${APFEL_PROXY_PORT:-${DEFAULT_PORT}}"
PROXY_URL="http://localhost:${PROXY_PORT}/v1"
MAX_PROMPT_TOKENS="${MAX_PROMPT_TOKENS:-120000}"

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
  echo "starting ${PROXY_SCRIPT} on :${PROXY_PORT}..." >&2
  env "${PORT_ENV}=${PROXY_PORT}" APFEL_MAX_TOOLS="${APFEL_MAX_TOOLS:-8}" \
    python3 "${HERE}/${PROXY_SCRIPT}" >/tmp/apfel-proxy.log 2>&1 &
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
export COPILOT_PROVIDER_MAX_PROMPT_TOKENS="${MAX_PROMPT_TOKENS}"
export COPILOT_PROVIDER_MAX_OUTPUT_TOKENS="512"
export COPILOT_OFFLINE="1"                      # skip GitHub auth/telemetry/web/auto-update

exec copilot "$@"
