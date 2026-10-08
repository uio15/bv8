#!/usr/bin/env python3
"""Minimal MCP Streamable-HTTP client for the Workspace MCP Bridge."""
import json, os, sys, urllib.request, urllib.error

URL = os.environ.get(
    "MCP_URL",
    "https://preflight-lyrics-fleshy.ngrok-free.dev/mcp/"
    "d40c349c560f231c45567ac5aad3b27777af07c8a1874cb8529c9485f93fd359",
)
SESS_FILE = os.environ.get("MCP_SESS_FILE", "/tmp/mcp_session.txt")


def _post(payload, session=None, timeout=180):
    data = json.dumps(payload).encode("utf-8")
    req = urllib.request.Request(URL, data=data, method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("ngrok-skip-browser-warning", "true")
    req.add_header("User-Agent", "cline-mcp-client/1.0")
    if session:
        req.add_header("mcp-session-id", session)
    with urllib.request.urlopen(req, timeout=timeout) as r:
        sid = r.headers.get("mcp-session-id")
        body = r.read().decode("utf-8", "replace")
    return sid, body


def _parse(body):
    body = body.strip()
    if not body:
        return []
    msgs = []
    if body.startswith("{"):
        try:
            return [json.loads(body)]
        except json.JSONDecodeError:
            pass
    for chunk in body.split("\n\n"):
        for line in chunk.splitlines():
            if line.startswith("data:"):
                raw = line[5:].strip()
                if raw:
                    try:
                        msgs.append(json.loads(raw))
                    except json.JSONDecodeError:
                        pass
    return msgs


def load_session():
    if os.path.exists(SESS_FILE):
        return open(SESS_FILE).read().strip() or None
    return None


def save_session(sid):
    if sid:
        with open(SESS_FILE, "w") as fh:
            fh.write(sid)


def initialize(force=False):
    sid = None if force else load_session()
    if sid:
        return sid
    payload = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-06-18",
            "capabilities": {},
            "clientInfo": {"name": "cline-sandbox", "version": "1.0.0"},
        },
    }
    sid, body = _post(payload)
    save_session(sid)
    notif = {"jsonrpc": "2.0", "method": "notifications/initialized"}
    req = urllib.request.Request(URL, data=json.dumps(notif).encode(), method="POST")
    req.add_header("Content-Type", "application/json")
    req.add_header("Accept", "application/json, text/event-stream")
    req.add_header("ngrok-skip-browser-warning", "true")
    req.add_header("mcp-session-id", sid)
    try:
        urllib.request.urlopen(req, timeout=30).read()
    except Exception as exc:  # noqa: BLE001
        sys.stderr.write("initialized-notify: %s\n" % exc)
    return sid


_next_id = [10]


def rpc(method, params=None, timeout=180):
    sid = initialize()
    _next_id[0] += 1
    payload = {"jsonrpc": "2.0", "id": _next_id[0], "method": method}
    if params is not None:
        payload["params"] = params
    sid2, body = _post(payload, sid, timeout=timeout)
    save_session(sid2 or sid)
    msgs = _parse(body)
    out = [m for m in msgs if m.get("id") == payload["id"]]
    if not out:
        out = msgs
    if not out:
        return {"error": "empty response", "raw": body[:2000]}
    msg = out[0]
    if "error" in msg:
        return {"error": msg["error"]}
    return msg.get("result", msg)


def call(tool, args=None, timeout=180):
    return rpc("tools/call", {"name": tool, "arguments": args or {}}, timeout)


def main():
    cmd = sys.argv[1] if len(sys.argv) > 1 else "tools"
    if cmd == "init":
        print(initialize(force=True))
    elif cmd == "tools":
        res = rpc("tools/list")
        print(json.dumps(res, ensure_ascii=False, indent=2))
    elif cmd == "raw":
        print(json.dumps(rpc(sys.argv[2], json.loads(sys.argv[3]) if len(sys.argv) > 3 else None), ensure_ascii=False, indent=2))
    else:  # call <tool> [json]
        args = json.loads(sys.argv[3]) if len(sys.argv) > 3 else {}
        timeout = int(os.environ.get("MCP_TIMEOUT", "180"))
        res = call(sys.argv[2] if cmd != "call" else sys.argv[2], args, timeout=timeout)
        print(json.dumps(res, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
