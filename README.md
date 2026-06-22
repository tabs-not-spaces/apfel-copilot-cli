<p align="center">
  <img src="logo.svg" alt="apfel-copilot-cli logo" width="160" height="160">
</p>

<h1 align="center">apfel-copilot</h1>

<p align="center">Make GitHub Copilot CLI talk to <b>apfel</b> (Apple on-device LLM) instead of cloud.</p>

## What

Copilot CLI supports BYOK (bring your own model), and [apfel](https://github.com/Arthur-Ficial/apfel) runs a local OpenAI-compatible server. This project wires the two together.

The catch: apfel's context is **4096 tokens**, but a Copilot request is **~107k tokens** (226 tool schemas + a big system prompt). It doesn't fit.

The fix: `apfel_proxy.py` sits in the middle. It strips tools, trims the system prompt, and rolls history out to files, shrinking each request from ~107k down to ~2k so it fits.

The trade-off: with tools stripped, you get **chat only** - no file edits, no shell agent.

## Pre-reqs

- macOS 26.4+, Apple Silicon (M1+)
- Apple Intelligence ON
- `apfel` -> `brew install apfel` ([Arthur-Ficial/apfel](https://github.com/Arthur-Ficial/apfel))
- `copilot` (GitHub Copilot CLI)
- `python3`
- `pwsh` 7.4+ (only for `.ps1`)

## Use

Bash:

```bash
./copilot-apfel.sh -p "your prompt"
./copilot-apfel.sh                  # interactive
```

PowerShell:

```powershell
./copilot-apfel.ps1 -Prompt "your prompt"
./copilot-apfel.ps1                       # interactive
```

Both: auto-start apfel + proxy, set BYOK env, run copilot.

## Knobs (env)

| Var | Default | Means |
|-----|---------|-------|
| `APFEL_URL` | `http://localhost:11434/v1` | apfel server |
| `APFEL_PROXY_PORT` | `8898` | proxy port |
| `APFEL_SYS_CHARS` | `8000` | system prompt cap |
| `APFEL_MSG_CHARS` | `5000` | history window cap |
| `APFEL_OUTPUT_CAP` | `400` | max output tokens |

PowerShell flags: `-ApfelUrl`, `-ProxyPort`, `-Model`, `-MaxPromptTokens`, `-MaxOutputTokens`.

## Files

| File | Job |
|------|-----|
| `apfel_proxy.py` | shrink each req to fit 4096, log history |
| `copilot-apfel.sh` | bash launcher |
| `copilot-apfel.ps1` | PowerShell launcher |

## History logs

`~/.apfel-copilot/`
- `transcript.jsonl` = full history, nothing lost
- `dropped-context.jsonl` = turns trimmed off wire

## Limit

4096 too small for full agent. Chat works. Tools no. That = Apple cap, not bug.

## v2 (experimental) - full agent attempt

`apfel_proxy_v2.py` = keep tools alive via dynamic selection instead of stripping.

How: per turn, pick top-K relevant tools from the 226 (lexical tool-RAG), trim
system + history, forward only the slim set. Tools run CLI-side, model just emits
the call. Logs picks to `~/.apfel-copilot/tool-selection.jsonl`.

Run:

```bash
APFEL_MAX_TOOLS=8 python3 apfel_proxy_v2.py   # port 8899
# point Copilot CLI BYOK at http://localhost:8899/v1, tools enabled
```

Knobs: `APFEL_PROXY_V2_PORT`, `APFEL_MAX_TOOLS`, `APFEL_TOOL_TOKENS`,
`APFEL_SYS_TOKENS`, `APFEL_HIST_TOKENS`, `APFEL_OUTPUT_CAP`.

**Status: plumbing works, model does not.** Proven: 226 tools -> 1-8 selected,
fits 4096, streaming + agent loop reach the model. BUT `apple-foundationmodel`:
- invents tool names not in the set (`ls`, `echo`)
- emits `arguments` as array, not object -> CLI rejects
- echoes the tool-format text, drifts off-task
- fails even at `MAX_TOOLS=1`

Also some single tool schemas (e.g. `session_store_sql` ~4158 tok) exceed 4096
alone. Verdict: on-device 3B model can't reliably drive the agent protocol.
Needs constrained/grammar decoding or a stronger model. Use v1 chat for now.

## Credits

Built on [apfel](https://github.com/Arthur-Ficial/apfel) by Arthur-Ficial - the
UNIX tool + OpenAI-compatible server for Apple's on-device FoundationModels.


