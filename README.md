<p align="center">
  <img src="logo.svg" alt="apfel-copilot-cli logo" width="160" height="160">
</p>

<h1 align="center">apfel-copilot</h1>

<p align="center">Run GitHub Copilot CLI as a real agent against <b>apfel</b> (Apple on-device LLM) instead of the cloud.</p>

## What

Copilot CLI supports BYOK (bring your own model), and [apfel](https://github.com/Arthur-Ficial/apfel) runs Apple's on-device model as a local OpenAI-compatible server. This project wires the two together so the whole agent — file edits, shell, search — runs on your Mac, offline.

The catch: apfel's context is **4096 tokens**, but a raw Copilot request is **~107k tokens** (226 tool schemas + a big system prompt). It does not fit.

The fix: a proxy sits in the middle and fits every request into 4096. Two versions:

- **v2 (default) — full agent.** Instead of stripping tools, it drives them with apfel's `json_schema` response format (Apple **guided generation** = forced valid output). Per turn it routes (does this need a tool?), picks the tool, fills the args under a constrained schema, then hands Copilot CLI clean OpenAI `tool_calls`. Real file edits and shell.
- **v1 — chat only.** Strips tools, trims the prompt. Plain conversation, no agent. Kept as a fallback.

Honest caveat: the on-device model is small. The agent **structure** is reliable (valid tool calls, right files), but answer **quality** is model-limited — it can miscount or summarise weakly. Good for local, private, offline work; not GPT-class.

## Pre-reqs

- macOS 26.4+, Apple Silicon (M1+)
- Apple Intelligence ON
- `apfel` -> `brew install apfel` ([Arthur-Ficial/apfel](https://github.com/Arthur-Ficial/apfel))
- `copilot` (GitHub Copilot CLI)
- `python3`
- `pwsh` 7.4+ (only for `.ps1`)

## Use

Both launchers default to **v2 (full agent)**, auto-start apfel + proxy, set BYOK env, run copilot.

### Interactive (REPL)

Omit the prompt — you drop into an interactive Copilot CLI session against apfel:

```bash
./copilot-apfel.sh                  # bash, v2 agent
```

```powershell
./copilot-apfel.ps1                 # PowerShell, v2 agent
```

First launch is slower: apfel cold-loads the on-device model (the launchers wait
up to ~30s for it). Leave the session open — keeping it running keeps apfel + the
proxy warm, so later turns are fast.

### One-shot prompt

```bash
./copilot-apfel.sh -p "list the .py files and tell me which is biggest"
APFEL_PROXY_VARIANT=v1 ./copilot-apfel.sh -p "explain TCP/IP"   # legacy chat
```

```powershell
./copilot-apfel.ps1 -Prompt "list the .py files and tell me which is biggest"
./copilot-apfel.ps1 -ProxyVariant v1 -Prompt "explain TCP/IP"   # legacy chat
```

### Let the agent edit / run things

Pass Copilot's allow flags. In PowerShell they go through `-CopilotArgs` (a bare
`--allow-all-paths` would otherwise bind to `-ProxyPort`):

```bash
./copilot-apfel.sh --allow-all --allow-all-paths -p "in config.py set DEBUG=False"
```

```powershell
./copilot-apfel.ps1 -Prompt "in config.py set DEBUG=False" -CopilotArgs '--allow-all','--allow-all-paths'
```

## How v2 fits 4096

1. **Clean** the incoming Copilot request (drop wrapper blocks, restate the real ask, grab cwd).
2. **Select** a small set of tools — always seed the core ones (bash/view/edit/grep/glob/create), add lexical matches up to `APFEL_MAX_TOOLS`.
3. **Route** with a constrained boolean + tool-enum schema (few-shot planner, no giant system prompt → not tool-trigger-happy).
4. **Fill args** with a flat per-tool schema; all kept fields marked required so guided generation fills every one. Integer fields coerced to number (Apple quirk). Execution-control noise dropped.
5. **Synthesise** proper OpenAI `tool_calls` (stream + non-stream). Loop guards (`_recent_calls`, `MAX_TOOL_CALLS`) stop repeat spirals.

The proxy advertises a **large** prompt window to Copilot CLI (`120000`) so the CLI never panic-compacts; it fits everything to 4096 internally.

## Knobs (env)

| Var | Default | Means |
|-----|---------|-------|
| `APFEL_URL` | `http://localhost:11434/v1` | apfel server |
| `APFEL_PROXY_VARIANT` | `v2` | `v1` (chat) or `v2` (agent) |
| `APFEL_PROXY_PORT` | `8899` v2 / `8898` v1 | proxy port |
| `MAX_PROMPT_TOKENS` | `120000` | window advertised to CLI (stops auto-compact) |
| `APFEL_MAX_TOOLS` | `8` | v2: tools selected per turn |
| `APFEL_MAX_TOOL_CALLS` | `8` | v2: agent-loop circuit breaker |
| `APFEL_TEMPERATURE` | `0.0` | v2 sampling |
| `APFEL_DEBUG` | off | v2: log each turn's route/args |
| `APFEL_CAPTURE` | off | v2: dump raw requests to disk |

PowerShell flags: `-ProxyVariant`, `-MaxTools`, `-ApfelUrl`, `-ProxyPort`, `-Model`, `-MaxPromptTokens`, `-MaxOutputTokens`.

## Files

| File | Job |
|------|-----|
| `apfel_proxy_v2.py` | **default** — constrained-decoding agent bridge |
| `apfel_proxy.py` | legacy — chat only, strips tools |
| `copilot-apfel.sh` | bash launcher |
| `copilot-apfel.ps1` | PowerShell launcher |

## History logs

`~/.apfel-copilot/`
- `transcript.jsonl` = full history, nothing lost
- `dropped-context.jsonl` = turns trimmed off wire
- `tool-selection.jsonl` = which tools got picked per turn

## Limit

4096 is tiny. v2 makes the agent **work** by constraining the model instead of trusting it — structure is solid, but a 3B on-device model won't match a frontier model on hard reasoning. That's the Apple cap, not a bug.

## Credits

Built on [apfel](https://github.com/Arthur-Ficial/apfel) by Arthur-Ficial — the
UNIX tool + OpenAI-compatible server for Apple's on-device FoundationModels.
