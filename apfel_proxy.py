#!/usr/bin/env python3
"""
apfel_proxy.py - Context-fitting proxy between GitHub Copilot CLI and apfel.

WHY:
  apfel (Apple FoundationModels) has a HARD 4096-token context window.
  Copilot CLI sends ~107k tokens per request:
    - 226 tool schemas  (~103k tokens)   <- the real hog
    - system prompt      (~6.2k tokens)
    - conversation history (grows over time)
  None of that fits 4096. This proxy rewrites each request to fit, and
  offloads the FULL transcript + dropped context to local files so nothing
  is silently lost.

WHAT IT DOES per /v1/chat/completions request:
  1. Strips `tools` / `tool_choice`  -> reclaims ~103k tokens.
  2. Truncates the system prompt to SYS_CHAR_BUDGET.
  3. Keeps a most-recent-first window of history that fits MSG_CHAR_BUDGET;
     older turns are dropped from the wire but appended to a local JSONL log.
  4. Caps max_tokens so prompt + output stay under the 4096 window.

LIMITATION:
  Stripping tools means the agent cannot edit files or run shell commands.
  This yields a working *chat* against the on-device model, not the full
  Copilot agent. The 6.2k system prompt is also truncated, so agent
  behavior is degraded by design - this is the price of a 4096 window.

USAGE:
  python3 apfel_proxy.py            # listens on :8898, forwards to :11434
  then point Copilot CLI at  http://localhost:8898/v1  (see copilot-apfel.sh)
"""
import http.server
import urllib.request
import urllib.error
import json
import os
import sys
import time

UPSTREAM = os.environ.get("APFEL_UPSTREAM", "http://localhost:11434")
LISTEN_PORT = int(os.environ.get("APFEL_PROXY_PORT", "8898"))

# Char budgets (~4 chars/token). 4096 ctx total.
SYS_CHAR_BUDGET = int(os.environ.get("APFEL_SYS_CHARS", "8000"))   # ~2000 tok
MSG_CHAR_BUDGET = int(os.environ.get("APFEL_MSG_CHARS", "5000"))   # ~1250 tok
OUTPUT_CAP = int(os.environ.get("APFEL_OUTPUT_CAP", "400"))        # ~400 tok

LOG_DIR = os.environ.get("APFEL_LOG_DIR", os.path.expanduser("~/.apfel-copilot"))
os.makedirs(LOG_DIR, exist_ok=True)
TRANSCRIPT = os.path.join(LOG_DIR, "transcript.jsonl")   # full history, nothing lost
DROPPED = os.path.join(LOG_DIR, "dropped-context.jsonl")  # what was trimmed off the wire


def _append(path, obj):
    with open(path, "a") as f:
        f.write(json.dumps(obj) + "\n")


def fit_messages(messages):
    """Truncate system prompt; keep newest messages that fit MSG_CHAR_BUDGET."""
    system = None
    convo = []
    for m in messages:
        if m.get("role") == "system" and system is None:
            system = m
        else:
            convo.append(m)

    if system is not None and isinstance(system.get("content"), str):
        if len(system["content"]) > SYS_CHAR_BUDGET:
            system = dict(system)
            system["content"] = system["content"][:SYS_CHAR_BUDGET] + "\n[system prompt truncated to fit 4096 ctx]"

    kept, dropped, used = [], [], 0
    for m in reversed(convo):
        c = m.get("content")
        size = len(c) if isinstance(c, str) else len(json.dumps(c))
        if used + size <= MSG_CHAR_BUDGET:
            kept.append(m)
            used += size
        else:
            dropped.append(m)
    kept.reverse()

    out = ([system] if system is not None else []) + kept
    return out, dropped


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
            sys.stderr.write(f"rewrite parse-err: {e}\n")
            return body

        p.pop("tools", None)
        p.pop("tool_choice", None)

        original = p.get("messages", [])
        # log the newest user turn to the durable transcript
        for m in original:
            if m.get("role") == "user":
                _append(TRANSCRIPT, {"ts": time.time(), **m})

        fitted, dropped = fit_messages(original)
        for d in dropped:
            _append(DROPPED, {"ts": time.time(), **d})

        p["messages"] = fitted
        p["max_tokens"] = min(p.get("max_tokens") or OUTPUT_CAP, OUTPUT_CAP)

        new_body = json.dumps(p).encode()
        sys.stderr.write(
            f"[rewrite] msgs {len(original)}->{len(fitted)} "
            f"dropped={len(dropped)} ~{len(new_body)//4}tok\n"
        )
        return new_body

    def _forward(self, path, body):
        req = urllib.request.Request(UPSTREAM + path, data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in ("host", "content-length"):
                req.add_header(k, v)
        try:
            with urllib.request.urlopen(req) as r:
                data = r.read()
                self.send_response(r.status)
                self.send_header("content-type", "application/json")
                self.send_header("content-length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
        except urllib.error.HTTPError as e:
            data = e.read()
            sys.stderr.write(f"[upstream {e.code}] {data[:200]!r}\n")
            self.send_response(e.code)
            self.send_header("content-type", "application/json")
            self.send_header("content-length", str(len(data)))
            self.end_headers()
            self.wfile.write(data)
        except Exception as e:
            sys.stderr.write(f"[forward-err] {e}\n")
            self.send_response(502)
            self.end_headers()

    def log_message(self, *a):
        pass


if __name__ == "__main__":
    sys.stderr.write(
        f"apfel_proxy on :{LISTEN_PORT} -> {UPSTREAM}\n"
        f"  logs: {TRANSCRIPT}\n        {DROPPED}\n"
    )
    http.server.HTTPServer(("127.0.0.1", LISTEN_PORT), Handler).serve_forever()
