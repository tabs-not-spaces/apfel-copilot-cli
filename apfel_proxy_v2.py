#!/usr/bin/env python3
"""apfel_proxy_v2.py - Agent bridge for Copilot CLI -> apfel (Apple FoundationModels).

The problem
-----------
A raw Copilot CLI request is ~107k tokens (226 tool schemas + a big system
prompt) and apfel has a hard 4096-token context window. Worse, when handed an
OpenAI `tools` array the on-device model emits unreliable tool_calls: it invents
tool names, puts arguments under a generic `value` key, or returns `arguments`
as an array. The old v2 forwarded those broken calls verbatim, so the agent loop
never worked.

The fix: constrained decoding
-----------------------------
apfel supports OpenAI `response_format: json_schema`, which Apple implements with
*guided generation* - the model is FORCED to emit JSON matching the schema. That
makes structurally-valid output guaranteed. This proxy never relies on the
model's native tool_calls. Instead, per turn it runs up to three small,
schema-constrained calls and synthesises a clean OpenAI tool_call for the CLI:

  1. RAG        - lexically pick the few tools relevant to this turn (fits 4096).
  2. ROUTE      - constrained call: choose one tool name from an enum, or "none".
  3. ARGS       - constrained call against the chosen tool's own (sanitised)
                  parameter schema -> a valid arguments object.
     or TEXT    - if "none", a plain completion for a natural-language answer.

The proxy then returns a normal OpenAI response (streaming or not, matching the
request) so Copilot CLI runs its usual agent loop: execute tool -> feed result
back -> next turn. Tools execute CLI-side; the model only has to pick + fill.

Token estimate is chars/4 throughout (good enough for budgeting).
"""
import http.server
import urllib.request
import urllib.error
import collections
import json
import os
import re
import sys
import time
import uuid

UPSTREAM = os.environ.get("APFEL_UPSTREAM", "http://localhost:11434")
LISTEN_PORT = int(os.environ.get("APFEL_PROXY_V2_PORT", "8899"))
MODEL = os.environ.get("APFEL_MODEL", "apple-foundationmodel")

CTX_LIMIT = 4096
OUTPUT_RESERVE = int(os.environ.get("APFEL_OUTPUT_CAP", "400"))
SYS_TOKEN_BUDGET = int(os.environ.get("APFEL_SYS_TOKENS", "900"))
HIST_TOKEN_BUDGET = int(os.environ.get("APFEL_HIST_TOKENS", "1200"))
TOOL_TOKEN_BUDGET = int(os.environ.get("APFEL_TOOL_TOKENS", "1100"))
SCHEMA_TOKEN_BUDGET = int(os.environ.get("APFEL_SCHEMA_TOKENS", "700"))
MAX_TOOLS = int(os.environ.get("APFEL_MAX_TOOLS", "8"))
MAX_PROPS = int(os.environ.get("APFEL_MAX_PROPS", "12"))
TEMPERATURE = float(os.environ.get("APFEL_TEMPERATURE", "0.0"))
DEBUG = os.environ.get("APFEL_DEBUG", "") not in ("", "0", "false")

LOG_DIR = os.environ.get("APFEL_LOG_DIR", os.path.expanduser("~/.apfel-copilot"))
os.makedirs(LOG_DIR, exist_ok=True)
TRANSCRIPT = os.path.join(LOG_DIR, "transcript.jsonl")
SELECTION_LOG = os.path.join(LOG_DIR, "tool-selection.jsonl")
DROPPED = os.path.join(LOG_DIR, "dropped-context.jsonl")

_STOPWORDS = {
    "the", "a", "an", "to", "of", "in", "on", "for", "and", "or", "is", "are",
    "this", "that", "with", "use", "using", "please", "i", "you", "it", "me",
    "my", "your", "we", "can", "could", "would", "should", "do", "does", "did",
    "what", "which", "how", "from", "at", "by", "be", "as", "if", "then",
}


# --------------------------------------------------------------------------- #
# Token + text helpers
# --------------------------------------------------------------------------- #
def est_tokens(text):
    return len(text) // 4


def tokenize(text):
    return [w for w in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]+", (text or "").lower())
            if w not in _STOPWORDS and len(w) > 1]


def _append(path, obj):
    try:
        with open(path, "a") as f:
            f.write(json.dumps(obj) + "\n")
    except Exception:
        pass


def dbg(msg):
    if DEBUG:
        sys.stderr.write(msg if msg.endswith("\n") else msg + "\n")


# --------------------------------------------------------------------------- #
# Tool-RAG: pick the few relevant tools that fit the window
# --------------------------------------------------------------------------- #
def tool_fn(tool):
    return tool.get("function", tool)


def tool_text(tool):
    fn = tool_fn(tool)
    parts = [fn.get("name", ""), fn.get("description", "")]
    params = (fn.get("parameters") or {}).get("properties") or {}
    for pname, pdef in params.items():
        parts.append(pname)
        if isinstance(pdef, dict):
            parts.append(pdef.get("description", ""))
    return " ".join(parts)


def query_terms(messages, depth=4):
    terms = []
    for m in reversed(messages):
        role = m.get("role")
        if role in ("user", "assistant", "tool"):
            content = m.get("content")
            if isinstance(content, str):
                terms.extend(tokenize(content) * (3 if role == "user" else 1))
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                terms.extend(tokenize(fn.get("name", "")))
            depth -= 1
            if depth <= 0:
                break
    return terms


def referenced_tool_names(messages, depth=6):
    names = set()
    for m in list(reversed(messages))[:depth]:
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if fn.get("name"):
                names.add(fn["name"])
        if m.get("role") == "tool" and m.get("name"):
            names.add(m["name"])
    return names


CORE_EXACT = ("bash", "view", "edit", "str_replace", "create", "grep", "glob",
              "read_file", "write", "ls", "shell", "run")
CORE_HINTS = ("bash", "shell", "command", "view", "read", "cat", "glob",
              "find", "grep", "search", "edit", "str_replace", "replace",
              "write", "create", "apply_patch", "ls", "list")


def core_tools(tools, limit=6):
    """Always-available everyday tools, so RAG misses don't hide core actions.

    Exact, well-known names (bash, view, edit, ...) are preferred over fuzzy
    substring hits like `list_bash`/`read_bash` so the primary tool wins.
    """
    by_name = {(tool_fn(t).get("name") or ""): t for t in tools}
    picked, seen = [], set()
    for name in CORE_EXACT:
        if name in by_name and name not in seen:
            picked.append(by_name[name])
            seen.add(name)
        if len(picked) >= limit:
            return picked
    for tool in tools:
        name = (tool_fn(tool).get("name") or "")
        if name in seen:
            continue
        if any(h in name.lower() for h in CORE_HINTS):
            picked.append(tool)
            seen.add(name)
        if len(picked) >= limit:
            break
    return picked


def select_tools(tools, messages):
    if not tools:
        return [], {"selected": [], "scored": 0}

    q = query_terms(messages)
    qcount = {}
    for t in q:
        qcount[t] = qcount.get(t, 0) + 1

    pinned = referenced_tool_names(messages)
    scored = []
    for tool in tools:
        name = tool_fn(tool).get("name", "")
        terms = set(tokenize(tool_text(tool)))
        score = sum(qcount.get(t, 0) for t in terms)
        if name in pinned:
            score += 1000
        scored.append((score, est_tokens(json.dumps(tool)), name, tool))

    scored.sort(key=lambda x: x[0], reverse=True)

    selected, used, names = [], 0, set()
    # Seed with core tools so listing/reading/editing is always reachable.
    for tool in core_tools(tools):
        name = tool_fn(tool).get("name")
        if name in names:
            continue
        size = est_tokens(json.dumps(tool))
        selected.append(tool)
        names.add(name)
        used += size
        if len(selected) >= MAX_TOOLS:
            break

    for score, size, name, tool in scored:
        if len(selected) >= MAX_TOOLS:
            break
        if name in names:
            continue
        if used + size > TOOL_TOKEN_BUDGET and score < 1000:
            continue
        selected.append(tool)
        names.add(name)
        used += size

    info = {
        "selected": [tool_fn(s).get("name") for s in selected],
        "scored": len(scored),
        "tool_tokens": used,
        "pinned": sorted(pinned),
    }
    return selected, info


# --------------------------------------------------------------------------- #
# Schema sanitiser: Copilot tool params -> apfel/Apple-Generable-safe json_schema
# --------------------------------------------------------------------------- #
_ALLOWED_TYPES = {"string", "number", "boolean", "array", "object"}


def sanitize_schema(node, depth=0):
    """Return a minimal schema apfel's guided generation can deserialise.

    Apple's Generable rejects many JSON-Schema keywords and the `integer` type,
    so we keep only type/properties/items/required/enum/description, coerce
    integer->number, and drop anyOf/oneOf/$ref/etc. Unknown shapes degrade to
    string so the model can still answer.
    """
    if not isinstance(node, dict):
        return {"type": "string"}

    t = node.get("type")
    if isinstance(t, list):
        t = next((x for x in t if x in _ALLOWED_TYPES or x == "integer"), "string")
    if t == "integer":
        t = "number"
    if t not in _ALLOWED_TYPES:
        # anyOf/oneOf/enum-only/untyped -> best-effort
        if node.get("enum"):
            t = "string"
        elif node.get("properties"):
            t = "object"
        elif node.get("items"):
            t = "array"
        else:
            t = "string"

    out = {"type": t}
    desc = node.get("description")
    if isinstance(desc, str) and desc:
        out["description"] = desc[:120]
    if node.get("enum") and t == "string":
        vals = [v for v in node["enum"] if isinstance(v, (str, int, float, bool))]
        if vals:
            out["enum"] = [str(v) for v in vals][:20]

    if t == "object" and depth < 3:
        props = node.get("properties") or {}
        required = [r for r in (node.get("required") or []) if r in props]
        # required first, then a few extras, capped to keep tokens small
        ordered = required + [k for k in props if k not in required]
        kept = {}
        for k in ordered[:MAX_PROPS]:
            kept[k] = sanitize_schema(props[k], depth + 1)
        out["properties"] = kept or {"value": {"type": "string"}}
        req = [r for r in required if r in kept]
        if req:
            out["required"] = req
    elif t == "object":
        out["type"] = "string"
        out.pop("properties", None)
    elif t == "array":
        items = node.get("items")
        out["items"] = sanitize_schema(items, depth + 1) if isinstance(items, dict) \
            else {"type": "string"}

    return out


def shrink_to_budget(schema):
    """Drop optional properties until the schema fits SCHEMA_TOKEN_BUDGET."""
    if est_tokens(json.dumps(schema)) <= SCHEMA_TOKEN_BUDGET:
        return schema
    props = schema.get("properties") or {}
    required = set(schema.get("required") or [])
    optional = [k for k in props if k not in required]
    while optional and est_tokens(json.dumps(schema)) > SCHEMA_TOKEN_BUDGET:
        props.pop(optional.pop(), None)
    return schema


NOISE_PARAMS = {"shellid", "mode", "detach", "initial_wait", "timeout",
                "async", "background", "cwd", "env", "sessionid", "wait",
                "head_limit", "offset", "limit"}


def tool_arg_schema(tool):
    params = tool_fn(tool).get("parameters") or {"type": "object", "properties": {}}
    schema = sanitize_schema(params)
    if schema.get("type") != "object":
        schema = {"type": "object", "properties": {"value": {"type": "string"}}}
    if not schema.get("properties"):
        schema["properties"] = {"value": {"type": "string"}}
    # Reliability: keep required fields plus the meaningful content optionals
    # (old_str/new_str/content/...), but drop execution-control noise the small
    # model otherwise fills with copied example/schema text.
    props = schema["properties"]
    required = schema.get("required") or []
    kept = {}
    for k, v in props.items():
        if k in required:
            kept[k] = v
        elif k.lower() not in NOISE_PARAMS:
            kept[k] = v
    schema["properties"] = kept or {"value": {"type": "string"}}
    # Apple guided generation only forces *required* fields; optional ones get
    # skipped (e.g. edit emits only `path`). We already pruned to meaningful
    # props, so mark them all required to force the model to fill each one.
    schema["required"] = list(schema["properties"].keys())
    return shrink_to_budget(schema)


def clean_desc(text, limit=160):
    """First useful sentence of a tool description, minus code-block examples.

    Tool descriptions embed examples (e.g. grep's `interface{}`) that the small
    model otherwise copies verbatim into arguments, so we strip them.
    """
    if not isinstance(text, str):
        return ""
    text = text.split("```")[0]
    for marker in ("\nExample", "\ne.g.", " e.g.", "\n<", "\n*", "\n-"):
        idx = text.find(marker)
        if idx > 0:
            text = text[:idx]
    text = " ".join(text.split())
    return text[:limit]


# --------------------------------------------------------------------------- #
# Context compaction
# --------------------------------------------------------------------------- #
_WRAP_BLOCK = re.compile(
    r"<(current_datetime|system_reminder|system-reminder|system_notification|"
    r"environment_context|sql_tables|todo_status)\b[^>]*>.*?</\1>",
    re.I | re.S)
_WRAP_SELFCLOSE = re.compile(
    r"<(current_datetime|system_reminder|system-reminder|system_notification|"
    r"environment_context|sql_tables|todo_status)\b[^>]*/?>", re.I)


def clean_user_text(text):
    """Strip Copilot CLI wrapper blocks/noise so the small model sees the ask.

    These wrappers (current datetime, system reminders, sql table hints, ...)
    carry no task intent but distract the small model into emitting placeholder
    arguments, so we remove the blocks entirely, content included.
    """
    if not isinstance(text, str):
        return ""
    text = _WRAP_BLOCK.sub(" ", text)
    text = _WRAP_SELFCLOSE.sub(" ", text)
    text = re.sub(r"<[^>]+>", " ", text)          # any other stray tags
    return " ".join(text.split()).strip()


_CWD_RE = re.compile(r"current working directory:\s*(\S+)", re.I)


def extract_cwd(messages):
    for m in messages:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            hit = _CWD_RE.search(m["content"])
            if hit:
                return hit.group(1)
    return ""


def latest_user_request(messages):
    for m in reversed(messages):
        if m.get("role") == "user":
            return clean_user_text(_msg_text(m))[:400]
    return ""


def _msg_text(m):
    c = m.get("content")
    if isinstance(c, str):
        return c
    if isinstance(c, list):
        return " ".join(p.get("text", "") for p in c if isinstance(p, dict))
    return json.dumps(m)[:400]


def history_only(messages):
    """Newest non-system messages, flattened, within HIST_TOKEN_BUDGET."""
    convo = [m for m in messages if m.get("role") != "system"]
    kept, dropped, used = [], [], 0
    for m in reversed(convo):
        role = m.get("role")
        text = _msg_text(m)
        if role == "user":
            text = clean_user_text(text)
        elif role == "tool":
            text = "tool result: " + text
        elif role == "assistant" and m.get("tool_calls"):
            calls = ", ".join((tc.get("function") or {}).get("name", "?")
                              for tc in m["tool_calls"])
            text = (text + " " if text else "") + "(called: " + calls + ")"
        flat = {"role": "user" if role == "tool" else role, "content": text or ""}
        size = est_tokens(flat["content"])
        if used + size <= HIST_TOKEN_BUDGET or not kept:
            kept.append(flat)
            used += size
        else:
            dropped.append(m)
    kept.reverse()
    return kept, dropped


# --------------------------------------------------------------------------- #
# Upstream calls
# --------------------------------------------------------------------------- #
def upstream_chat(messages, response_format=None, max_tokens=None, headers=None):
    payload = {
        "model": MODEL,
        "messages": messages,
        "stream": False,
        "temperature": TEMPERATURE,
        "max_tokens": max_tokens or OUTPUT_RESERVE,
    }
    if response_format is not None:
        payload["response_format"] = response_format
    data = json.dumps(payload).encode()
    req = urllib.request.Request(
        UPSTREAM + "/v1/chat/completions", data=data, method="POST")
    req.add_header("content-type", "application/json")
    if headers:
        for k, v in headers.items():
            if k.lower() not in ("host", "content-length", "content-type", "accept"):
                req.add_header(k, v)
    with urllib.request.urlopen(req) as r:
        return json.loads(r.read())


def first_message(resp):
    try:
        return resp["choices"][0]["message"]
    except (KeyError, IndexError, TypeError):
        return {}


# --------------------------------------------------------------------------- #
# Agent turn: RAG -> route -> args/text
# --------------------------------------------------------------------------- #
ROUTE_HINT = (
    "\n\nYou are an agent. Decide the single best next step to satisfy the "
    "latest user request, given the conversation and any tool results so far. "
    "Choose exactly one tool by name, or \"none\" to reply in plain text when no "
    "tool is needed or the task is already complete."
)


def forced_tool_name(p, selected_names):
    tc = p.get("tool_choice")
    if isinstance(tc, dict):
        name = (tc.get("function") or {}).get("name")
        if name in selected_names:
            return name
    return None


def prior_tool_calls(messages):
    """(name, canonical-args) of assistant tool_calls already in the history."""
    sigs = []
    for m in messages:
        if m.get("role") == "assistant":
            for tc in (m.get("tool_calls") or []):
                fn = tc.get("function") or {}
                try:
                    args = json.loads(fn.get("arguments") or "{}")
                except Exception:
                    args = fn.get("arguments")
                sigs.append((fn.get("name"), json.dumps(args, sort_keys=True)))
    return sigs


MAX_TOOL_CALLS = int(os.environ.get("APFEL_MAX_TOOL_CALLS", "8"))
REPEAT_LIMIT = int(os.environ.get("APFEL_REPEAT_LIMIT", "2"))

# In-process guard against agent loops: a weak model often re-emits the exact
# same tool_call even after a result (or when the call errors and is not
# persisted into history). We remember recently emitted signatures and, once one
# repeats REPEAT_LIMIT times, force a text answer to break the loop.
_recent_calls = collections.deque(maxlen=24)


def _emit_signature(name, args):
    return name + ":" + json.dumps(args, sort_keys=True)


def run_turn(p, headers):
    messages = p.get("messages", [])
    all_tools = p.get("tools", []) or []

    for m in messages:
        if m.get("role") == "user":
            _append(TRANSCRIPT, {"ts": time.time(), **{k: m.get(k) for k in ("role", "content")}})

    selected, sel_info = select_tools(all_tools, messages)
    selected_names = sel_info.get("selected", [])
    by_name = {tool_fn(t).get("name"): t for t in selected}

    history, dropped = history_only(messages)
    for d in dropped:
        _append(DROPPED, {"ts": time.time(), "role": d.get("role")})

    seen_calls = prior_tool_calls(messages)
    chosen = forced_tool_name(p, selected_names)
    ask = latest_user_request(messages)
    cwd = extract_cwd(messages)
    dbg(f"[v2] ask={ask!r} selected={selected_names}")

    # Circuit breaker: too many tool calls already -> force a final answer so the
    # CLI agent loop cannot spin forever on a weak model.
    force_text = len(seen_calls) >= MAX_TOOL_CALLS

    # ----- ROUTE ----------------------------------------------------------- #
    if not force_text and chosen is None and selected_names:
        planner = (
            "You are the planner for a coding agent. Pick the next step.\n"
            "Use a tool when the user wants to inspect or change the system: run "
            "a command, list files, read or edit a file, search code.\n"
            "Use none (needs_tool=false) when the request is general knowledge, "
            "conversation, simple arithmetic, or is already answerable from text "
            "and tool results already in the conversation.\n"
            "Examples: 'list the files' -> bash; 'open config.py' -> view; "
            "'replace X with Y in app.py' -> edit; 'search for TODO' -> grep; "
            "'how many files are there?' (after a listing) -> none; "
            "'what is the capital of France?' -> none; 'what is 2+2?' -> none.\n"
            "Tools available this turn: " + ", ".join(selected_names) + ".\n"
            "Latest user request: " + ask
        )
        route_msgs = [{"role": "system", "content": planner}] + history
        route_schema = {
            "type": "json_schema",
            "json_schema": {
                "name": "route",
                "schema": {
                    "type": "object",
                    "properties": {
                        "needs_tool": {"type": "boolean"},
                        "tool": {"type": "string", "enum": selected_names + ["none"]},
                    },
                    "required": ["needs_tool", "tool"],
                },
            },
        }
        try:
            resp = upstream_chat(route_msgs, route_schema, max_tokens=40, headers=headers)
            content = first_message(resp).get("content") or "{}"
            decision = json.loads(content)
            chosen = decision.get("tool") if decision.get("needs_tool") else None
        except Exception as e:
            dbg(f"[v2 route-err] {e}")
            chosen = None
        if chosen not in by_name:
            chosen = None

    log = {
        "ts": time.time(), "tools_in": len(all_tools),
        "selected": selected_names, "chosen": chosen,
    }
    _append(SELECTION_LOG, log)

    # ----- ARGS ------------------------------------------------------------ #
    if not force_text and chosen and chosen in by_name:
        tool = by_name[chosen]
        arg_schema = tool_arg_schema(tool)
        desc = clean_desc(tool_fn(tool).get("description", ""))
        shell_hint = ""
        if "command" in (arg_schema.get("properties") or {}):
            shell_hint = ("\nThe `command` field is the exact shell command line to "
                          "run (for example \"cat alpha.txt\" or \"ls -la\"); it is "
                          "never the tool's own name.")
        path_hint = ""
        if "path" in (arg_schema.get("properties") or {}) or "file" in str(arg_schema):
            path_hint = ("\nWorking directory: %s. Use paths exactly as the user "
                         "wrote them (relative to the working directory); do not "
                         "prepend /tmp or invent directories." % (cwd or "."))
        args_sys = (
            "You produce the arguments to call the tool `%s`. %s\n"
            "Latest user request: %s\n"
            "Fill every field from that request and the conversation, literally "
            "and exactly. Use real values from the conversation, never placeholders "
            "like /path/to, and never copy the tool name or its description as a "
            "value.%s%s" % (chosen, desc, ask, shell_hint, path_hint)
        )
        arg_msgs = [{"role": "system", "content": args_sys}] + history
        rf = {"type": "json_schema",
              "json_schema": {"name": "args", "schema": arg_schema}}
        try:
            resp = upstream_chat(arg_msgs, rf, max_tokens=OUTPUT_RESERVE, headers=headers)
            raw = first_message(resp).get("content") or "{}"
            args = json.loads(raw)
            if not isinstance(args, dict):
                args = {"value": args}
        except Exception as e:
            dbg(f"[v2 args-err] {e}")
            args = {}

        sig = (chosen, json.dumps(args, sort_keys=True))
        emit_sig = _emit_signature(chosen, args)
        repeats = _recent_calls.count(emit_sig)
        if sig in seen_calls or repeats >= REPEAT_LIMIT:
            # Identical call already made / looping -> the result is already in
            # context (or never will be). Break the loop by answering instead.
            dbg(f"[v2] repeat x{repeats} {chosen}({json.dumps(args)}) -> force text")
        else:
            _recent_calls.append(emit_sig)
            dbg(f"[v2] -> tool_call {chosen}({json.dumps(args)})")
            return ("tool_call", chosen, args)

    # ----- TEXT ------------------------------------------------------------ #
    text_sys = (
        "You are a concise, helpful terminal coding assistant. Answer the user "
        "directly using the conversation and any tool results above. If tool "
        "results already contain the answer, summarise them. Do not describe "
        "tools or mention that you are an AI."
    )
    text_msgs = [{"role": "system", "content": text_sys}] + history
    try:
        resp = upstream_chat(text_msgs, None, max_tokens=OUTPUT_RESERVE, headers=headers)
        text = first_message(resp).get("content") or ""
    except Exception as e:
        dbg(f"[v2 text-err] {e}")
        text = ""
    dbg(f"[v2] -> text ({len(text)} chars)")
    return ("text", None, text)


# --------------------------------------------------------------------------- #
# OpenAI response synthesis (stream + non-stream)
# --------------------------------------------------------------------------- #
def _ids():
    return ("chatcmpl-" + uuid.uuid4().hex[:24], int(time.time()))


def build_completion(kind, name, payload):
    cid, created = _ids()
    if kind == "tool_call":
        message = {
            "role": "assistant",
            "content": None,
            "tool_calls": [{
                "id": "call_" + uuid.uuid4().hex[:24],
                "type": "function",
                "function": {"name": name, "arguments": json.dumps(payload)},
            }],
        }
        finish = "tool_calls"
    else:
        message = {"role": "assistant", "content": payload}
        finish = "stop"
    return {
        "id": cid, "object": "chat.completion", "created": created, "model": MODEL,
        "choices": [{"index": 0, "message": message, "finish_reason": finish, "logprobs": None}],
        "usage": {"prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0},
    }


def sse_chunks(kind, name, payload):
    cid, created = _ids()

    def chunk(delta, finish=None):
        obj = {
            "id": cid, "object": "chat.completion.chunk", "created": created,
            "model": MODEL,
            "choices": [{"index": 0, "delta": delta, "finish_reason": finish, "logprobs": None}],
        }
        return ("data: " + json.dumps(obj) + "\n\n").encode()

    yield chunk({"role": "assistant"})
    if kind == "tool_call":
        yield chunk({"tool_calls": [{
            "index": 0,
            "id": "call_" + uuid.uuid4().hex[:24],
            "type": "function",
            "function": {"name": name, "arguments": json.dumps(payload)},
        }]})
        yield chunk({}, finish="tool_calls")
    else:
        yield chunk({"content": payload})
        yield chunk({}, finish="stop")
    yield b"data: [DONE]\n\n"


# --------------------------------------------------------------------------- #
# HTTP handler
# --------------------------------------------------------------------------- #
class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self._passthrough(self.path, None)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = self.rfile.read(n)
        if self.path.endswith("/chat/completions"):
            self._handle_chat(body)
        else:
            self._passthrough(self.path, body)

    def _handle_chat(self, body):
        try:
            p = json.loads(body)
        except Exception as e:
            dbg(f"[v2] parse-err {e}")
            return self._passthrough(self.path, body)

        if os.environ.get("APFEL_CAPTURE"):
            _append(os.path.join(LOG_DIR, "raw-requests.jsonl"),
                    {"ts": time.time(), "request": p})

        want_stream = bool(p.get("stream"))
        try:
            kind, name, payload = run_turn(p, dict(self.headers))
        except Exception as e:
            sys.stderr.write(f"[v2 turn-err] {e}\n")
            kind, name, payload = "text", None, "(apfel proxy error)"

        if want_stream:
            self.send_response(200)
            self.send_header("content-type", "text/event-stream")
            self.send_header("cache-control", "no-cache")
            self.end_headers()
            for chunk in sse_chunks(kind, name, payload):
                self.wfile.write(chunk)
                self.wfile.flush()
        else:
            data = json.dumps(build_completion(kind, name, payload)).encode()
            self.send_response(200)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)

    def _passthrough(self, path, body):
        req = urllib.request.Request(UPSTREAM + path, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length"):
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as r:
                data = r.read()
                self.send_response(r.status)
                self.send_header("content-type", r.headers.get("content-type", "application/json"))
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            self.send_response(e.code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            sys.stderr.write(f"[v2 passthrough-err] {e}\n")
            self.send_response(502)
            self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    sys.stderr.write(
        f"apfel_proxy_v2 (constrained-decoding agent) on :{LISTEN_PORT} -> {UPSTREAM}\n"
        f"  budgets: sys={SYS_TOKEN_BUDGET} hist={HIST_TOKEN_BUDGET} "
        f"tools={TOOL_TOKEN_BUDGET} schema={SCHEMA_TOKEN_BUDGET} "
        f"max_tools={MAX_TOOLS} out={OUTPUT_RESERVE}\n"
        f"  logs: {SELECTION_LOG}\n"
    )
    http.server.HTTPServer(("127.0.0.1", LISTEN_PORT), Handler).serve_forever()
