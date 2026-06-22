#!/usr/bin/env python3
"""
apfel_proxy_v2.py - Agent-capable context-fitting proxy for Copilot CLI -> apfel.

Unlike apfel_proxy.py (which strips ALL tools => chat only), v2 keeps the agent
alive by DYNAMICALLY SELECTING a small, relevant subset of tools per turn so the
request fits apfel's hard 4096-token window.

Pipeline per /v1/chat/completions request:
  1. Parse the request (messages + full ~226 tool schemas come IN the request).
  2. Tool-RAG: score every tool by lexical relevance to the latest user turn +
     recent context; keep the top-K that fit TOOL_TOKEN_BUDGET. Always retain
     tools referenced by recent tool_calls / tool results (loop continuity).
  3. Compact the system prompt to SYS_TOKEN_BUDGET.
  4. Fit the most-recent history into HIST_TOKEN_BUDGET; older turns are logged
     to disk (and later summarized by the chained summarizer instance).
  5. Forward the slimmed request to apfel; return its response verbatim so the
     CLI sees normal OpenAI tool_calls and runs its own agent loop.

Tools execute on the Copilot CLI side, so the on-device model only has to emit a
valid tool_call. Reliability of that emission on a small model is the main risk.

Token estimate is chars/4 throughout (good enough for budgeting).
"""
import http.server
import urllib.request
import urllib.error
import json
import os
import re
import sys
import time

UPSTREAM = os.environ.get("APFEL_UPSTREAM", "http://localhost:11434")
LISTEN_PORT = int(os.environ.get("APFEL_PROXY_V2_PORT", "8899"))

CTX_LIMIT = 4096
OUTPUT_RESERVE = int(os.environ.get("APFEL_OUTPUT_CAP", "400"))
SYS_TOKEN_BUDGET = int(os.environ.get("APFEL_SYS_TOKENS", "1400"))
HIST_TOKEN_BUDGET = int(os.environ.get("APFEL_HIST_TOKENS", "900"))
TOOL_TOKEN_BUDGET = int(os.environ.get("APFEL_TOOL_TOKENS", "1100"))
MAX_TOOLS = int(os.environ.get("APFEL_MAX_TOOLS", "8"))

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


def est_tokens(text):
    return len(text) // 4


def tokenize(text):
    return [w for w in re.findall(r"[a-zA-Z_][a-zA-Z0-9_]+", (text or "").lower())
            if w not in _STOPWORDS and len(w) > 1]


def tool_text(tool):
    """Flatten a tool schema into searchable text (name + desc + param names)."""
    fn = tool.get("function", tool)
    parts = [fn.get("name", ""), fn.get("description", "")]
    params = (fn.get("parameters") or {}).get("properties") or {}
    for pname, pdef in params.items():
        parts.append(pname)
        if isinstance(pdef, dict):
            parts.append(pdef.get("description", ""))
    return " ".join(parts)


def query_terms(messages, depth=4):
    """Build the retrieval query from the latest user turn + recent context."""
    terms = []
    weight = 1
    for m in reversed(messages):
        role = m.get("role")
        if role in ("user", "assistant", "tool"):
            content = m.get("content")
            if isinstance(content, str):
                # most-recent messages weigh more
                terms.extend(tokenize(content) * (3 if role == "user" else 1) * weight)
            depth -= 1
            if depth <= 0:
                break
    return terms


def referenced_tool_names(messages, depth=6):
    """Tool names appearing in recent tool_calls / tool messages (continuity)."""
    names = set()
    for m in list(reversed(messages))[:depth]:
        for tc in (m.get("tool_calls") or []):
            fn = tc.get("function") or {}
            if fn.get("name"):
                names.add(fn["name"])
        if m.get("role") == "tool" and m.get("name"):
            names.add(m["name"])
    return names


def select_tools(tools, messages):
    """Lexical top-K tool selection under TOOL_TOKEN_BUDGET."""
    if not tools:
        return [], {"selected": [], "scored": 0}

    q = query_terms(messages)
    qcount = {}
    for t in q:
        qcount[t] = qcount.get(t, 0) + 1

    pinned = referenced_tool_names(messages)
    scored = []
    for tool in tools:
        fn = tool.get("function", tool)
        name = fn.get("name", "")
        terms = set(tokenize(tool_text(tool)))
        score = sum(qcount.get(t, 0) for t in terms)
        if name in pinned:
            score += 1000  # guarantee continuity tools survive
        scored.append((score, est_tokens(json.dumps(tool)), name, tool))

    scored.sort(key=lambda x: x[0], reverse=True)

    selected, used = [], 0
    for score, size, name, tool in scored:
        if len(selected) >= MAX_TOOLS:
            break
        # always keep pinned tools even if budget is tight
        if used + size > TOOL_TOKEN_BUDGET and score < 1000:
            continue
        selected.append(tool)
        used += size

    info = {
        "selected": [s.get("function", s).get("name") for s in selected],
        "scored": len(scored),
        "tool_tokens": used,
        "pinned": sorted(pinned),
    }
    return selected, info


TOOL_CONTRACT = (
    "\n\nTOOL USE RULES (strict):\n"
    "- You may ONLY call tools from the provided tools list, by their exact name.\n"
    "- Never invent tool names. If no tool fits, answer in plain text.\n"
    "- Tool `arguments` MUST be a JSON object matching the schema, e.g. {\"path\": \".\"}.\n"
    "- Prefer the `bash` tool for shell actions like listing files (e.g. command: \"ls\").\n"
)


def compact_system(messages, selected_names):
    out = []
    saw_system = False
    contract = TOOL_CONTRACT
    if selected_names:
        contract += "Available tools this turn: " + ", ".join(selected_names) + ".\n"
    for m in messages:
        if m.get("role") == "system" and isinstance(m.get("content"), str):
            saw_system = True
            budget_chars = SYS_TOKEN_BUDGET * 4
            content = m["content"]
            if len(content) > budget_chars:
                content = content[:budget_chars] + "\n[system truncated]"
            m = dict(m)
            m["content"] = content + contract
        out.append(m)
    if not saw_system:
        out.insert(0, {"role": "system", "content": contract.strip()})
    return out


def fit_history(messages):
    """Keep system + newest non-system messages within HIST_TOKEN_BUDGET."""
    system = [m for m in messages if m.get("role") == "system"]
    convo = [m for m in messages if m.get("role") != "system"]

    kept, dropped, used = [], [], 0
    for m in reversed(convo):
        c = m.get("content")
        size = est_tokens(c if isinstance(c, str) else json.dumps(m))
        if used + size <= HIST_TOKEN_BUDGET or not kept:
            kept.append(m)
            used += size
        else:
            dropped.append(m)
    kept.reverse()
    return system + kept, dropped


def _append(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


class Handler(http.server.BaseHTTPRequestHandler):
    def do_GET(self):
        self._forward(self.path, None)

    def do_POST(self):
        n = int(self.headers.get("content-length", 0))
        body = self.rfile.read(n)
        if self.path.endswith("/chat/completions"):
            body = self._rewrite(body)
        self._forward(self.path, body)

    def _rewrite(self, body):
        try:
            p = json.loads(body)
        except Exception as e:
            sys.stderr.write(f"[v2] parse-err {e}\n")
            return body

        messages = p.get("messages", [])
        all_tools = p.get("tools", []) or []

        for m in messages:
            if m.get("role") == "user":
                _append(TRANSCRIPT, {"ts": time.time(), **m})

        selected, sel_info = select_tools(all_tools, messages)

        messages = compact_system(messages, sel_info.get("selected", []))
        messages, dropped = fit_history(messages)
        for d in dropped:
            _append(DROPPED, {"ts": time.time(), **d})

        p["messages"] = messages
        if selected:
            p["tools"] = selected
        else:
            p.pop("tools", None)
            p.pop("tool_choice", None)
        p["max_tokens"] = min(p.get("max_tokens") or OUTPUT_RESERVE, OUTPUT_RESERVE)

        new_body = json.dumps(p).encode()
        sel_info["total_tokens_est"] = len(new_body) // 4
        sel_info["tools_in"] = len(all_tools)
        sel_info["ts"] = time.time()
        _append(SELECTION_LOG, sel_info)
        sys.stderr.write(
            f"[v2] tools {len(all_tools)}->{len(selected)} "
            f"{sel_info['selected']} | ~{len(new_body)//4}tok\n"
        )
        return new_body

    def _forward(self, path, body):
        req = urllib.request.Request(UPSTREAM + path, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length"):
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as r:
                ctype = r.headers.get("content-type", "application/json")
                is_stream = "text/event-stream" in ctype
                self.send_response(r.status)
                self.send_header("content-type", ctype)
                if is_stream:
                    # pipe SSE chunks straight through; no content-length
                    self.send_header("cache-control", "no-cache")
                    self.end_headers()
                    while True:
                        chunk = r.read(1024)
                        if not chunk:
                            break
                        self.wfile.write(chunk)
                        self.wfile.flush()
                else:
                    data = r.read()
                    self.send_header("content-length", str(len(data)))
                    self.end_headers()
                    self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            sys.stderr.write(f"[v2 upstream {e.code}] {data[:200]!r}\n")
            self.send_response(e.code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            sys.stderr.write(f"[v2 forward-err] {e}\n")
            self.send_response(502)
            self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    sys.stderr.write(
        f"apfel_proxy_v2 on :{LISTEN_PORT} -> {UPSTREAM}\n"
        f"  budgets: sys={SYS_TOKEN_BUDGET} hist={HIST_TOKEN_BUDGET} "
        f"tools={TOOL_TOKEN_BUDGET} max_tools={MAX_TOOLS} out={OUTPUT_RESERVE}\n"
        f"  logs: {SELECTION_LOG}\n"
    )
    http.server.HTTPServer(("127.0.0.1", LISTEN_PORT), Handler).serve_forever()
