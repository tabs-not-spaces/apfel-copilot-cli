# Copilot instructions for apfel-copilot

## Project shape

No build system, no package manager, no test framework. The repo is three
standalone scripts plus docs. Python uses the **standard library only**
(`http.server`, `urllib`) — do not add third-party dependencies or a
requirements file. Keep it dependency-free.

## Architecture (read these together)

This is a BYOK bridge that lets GitHub Copilot CLI use a local model:

```
Copilot CLI  --(COPILOT_PROVIDER_* env, OpenAI wire)-->  proxy  -->  apfel --serve
```

- `copilot-apfel.sh` / `copilot-apfel.ps1` — launchers. They start `apfel --serve`
  and a proxy, export the `COPILOT_PROVIDER_*` BYOK env, then exec `copilot`.
- `apfel_proxy.py` — v1, **production**. Intercepts `POST /v1/chat/completions`,
  strips all tool schemas, truncates the system prompt, rolls history to files,
  and forwards a request that fits apfel's window. Result: chat only.
- `apfel_proxy_v2.py` — **experimental**. Instead of stripping tools, it does
  lexical tool-RAG to keep a relevant subset per turn so the agent can call tools.
  Pipeline works; the on-device model's tool-call fidelity does not (see README
  "v2" section). Keep v1 as the stable source of truth; do not fold v2 into it.

The proxy is the **only** interception point. It rewrites `/v1/chat/completions`
and passes every other path (e.g. `/v1/models`) straight through to apfel.

## The 4096-token constraint drives everything

apfel (Apple FoundationModels) has a hard 4096-token context window. A raw
Copilot request is ~107k tokens (226 tool schemas ~103k + system prompt ~6.2k).
All proxy logic exists to fit requests into 4096. Token estimates use a
**chars/4** approximation throughout — keep using it for budgeting; don't pull in
a tokenizer library.

## BYOK env contract (must stay in sync across both launchers)

Model id is `apple-foundationmodel`. Both launchers set:
`COPILOT_PROVIDER_BASE_URL` (the proxy URL), `COPILOT_PROVIDER_TYPE=openai`,
`COPILOT_PROVIDER_API_KEY` (dummy; apfel needs none),
`COPILOT_PROVIDER_WIRE_MODEL`, `COPILOT_PROVIDER_MODEL_ID`, `COPILOT_MODEL`,
`COPILOT_PROVIDER_MAX_PROMPT_TOKENS`, `COPILOT_PROVIDER_MAX_OUTPUT_TOKENS`,
`COPILOT_OFFLINE=1`. Change one launcher → change the other.

Proxy behaviour is tuned only through env vars (no config files): e.g.
`APFEL_PROXY_PORT`, `APFEL_SYS_CHARS`, `APFEL_MSG_CHARS`, `APFEL_OUTPUT_CAP`
(v1); `APFEL_MAX_TOOLS`, `APFEL_TOOL_TOKENS`, `APFEL_SYS_TOKENS`,
`APFEL_HIST_TOKENS`, `APFEL_PROXY_V2_PORT` (v2). Runtime artifacts (transcript,
dropped context, tool-selection log) are written to `~/.apfel-copilot/`.

## PowerShell conventions (enforced)

`copilot-apfel.ps1` must stay **PSScriptAnalyzer-clean**. House style:

- Every piece of logic lives in an advanced function with an approved verb,
  `[CmdletBinding()]`, `[OutputType()]`, and comment-based help.
- Fully configured `[Parameter()]` blocks with validation attributes
  (`[ValidateRange]`, `[ValidateNotNullOrEmpty]`, `[ValidateScript]`).
- **Splatting, not backtick line-continuation**, for multi-arg calls.
- No brace "hugging": expand `@{ ... }` hashtables and `{ ... }` blocks.
- `Write-Information`, not `Write-Host`.
- A dot-source guard (`if ($MyInvocation.InvocationName -ne '.')`) gates the
  entry point so functions can be dot-sourced and unit-tested.
- Do not expose a `-p` parameter — it collides with `-ProxyPort`. The prompt is
  surfaced as `-Prompt` and translated to `copilot -p` internally; raw passthrough
  goes through `-CopilotArgs`.

## Verify changes (there is no test suite)

- PowerShell lint: `pwsh -NoProfile -Command "Invoke-ScriptAnalyzer -Path ./copilot-apfel.ps1"`
  (install once: `Install-Module PSScriptAnalyzer -Scope CurrentUser`).
- Python syntax check: `python3 -c "import ast; ast.parse(open('apfel_proxy.py').read())"`.
- End-to-end smoke test (the real check): start apfel, start the proxy, then run
  one prompt through a launcher, e.g.
  `./copilot-apfel.sh -p "Reply in one word: ready?"` or
  `pwsh -File ./copilot-apfel.ps1 -Prompt "Reply in one word: ready?"`.
  A real token count in the footer (`↑ … ↓ …`) means the round-trip worked.

## Gotchas

- apfel is macOS-only (Apple Silicon, macOS 26.4+, Apple Intelligence on); it can
  die if its host process is reaped, so launchers re-check and restart it.
- Force `stream=false`/SSE-passthrough carefully: Copilot CLI may request a
  streaming response. v2 pipes `text/event-stream` through unbuffered; a
  non-streamed reply to a streaming client fails with
  "request ended without sending any chunks".
