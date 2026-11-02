#!/usr/bin/env python3
"""Minimal Streamable-HTTP MCP client for Workspace MCP Bridge.

The Cline tool panel has no native MCP registration, so this script speaks
JSON-RPC 2.0 over HTTP(S) to the bridge and keeps the session id on disk.

Config resolution order (first hit wins):
  1. --url / --state CLI flags
  2. env MCP_BRIDGE_URL (+ MCP_BRIDGE_STATE for the state file path)
  3. state file (default: <repo>/.mcp/state.json)

Subcommands:
  init                       initialize + notifications/initialized, persist session
  ping                       liveness check
  tools [--json] [--verbose] tools/list (table, or raw JSON / full listing)
  schema <tool>              print one tool's full definition
  call <tool> [--args J]     tools/call (also --args-file F, or stdin via --args -)
  rpc <method> [--params J]  escape hatch for raw methods
  reset                      forget the stored session id (keeps the URL)

Examples:
  python3 scripts/mcp_bridge_client.py init
  python3 scripts/mcp_bridge_client.py tools -v
  python3 scripts/mcp_bridge_client.py call list_directory --args '{"path":"."}'
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request

PROTOCOL_VERSION = "2025-06-18"
CLIENT_INFO = {"name": "cline-sandbox", "version": "1.0.0"}
DEFAULT_STATE = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".mcp", "state.json"
)
# ngrok free tunnels serve an HTML interstitial to browser-like clients.
BASE_HEADERS = {
    "ngrok-skip-browser-warning": "1",
    "User-Agent": "workspace-mcp-bridge-client/1.0",
}


class RpcError(RuntimeError):
    def __init__(self, payload: dict):
        self.payload = payload
        err = payload.get("error") or {}
        super().__init__("%s: %s" % (err.get("code"), err.get("message")))


class Bridge:
    def __init__(self, url: str, state_path: str, timeout: float = 180.0):
        self.url = url
        self.state_path = state_path
        self.timeout = timeout
        self.state = self._load()
        self._next_id = 0

    # ---------- state ----------
    def _load(self) -> dict:
        try:
            with open(self.state_path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except (OSError, ValueError):
            return {}

    def save(self) -> None:
        os.makedirs(os.path.dirname(self.state_path), exist_ok=True)
        with open(self.state_path, "w", encoding="utf-8") as fh:
            json.dump(self.state, fh, indent=2, ensure_ascii=False)

    # ---------- transport ----------
    def _post(self, payload: dict, session_id: str | None):
        body = json.dumps(payload).encode("utf-8")
        headers = dict(BASE_HEADERS)
        headers["Content-Type"] = "application/json"
        headers["Accept"] = "application/json, text/event-stream"
        headers["MCP-Protocol-Version"] = self.state.get("protocol_version", PROTOCOL_VERSION)
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        req = urllib.request.Request(self.url, data=body, headers=headers, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read().decode("utf-8", "replace")
                new_sid = resp.headers.get("mcp-session-id")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", "replace")[:2000]
            raise RuntimeError("HTTP %s from bridge: %s" % (exc.code, detail)) from exc
        return self._extract(raw, payload), new_sid

    @staticmethod
    def _extract(raw: str, payload: dict):
        """Return the JSON-RPC envelope matching our request id."""
        raw = raw.strip()
        if not raw:
            return None
        if raw.startswith("{"):
            doc = json.loads(raw)
            return doc if isinstance(doc, dict) else None
        want = payload.get("id")
        for line in raw.splitlines():
            if not line.startswith("data:"):
                continue
            chunk = line[5:].strip()
            if not chunk:
                continue
            try:
                doc = json.loads(chunk)
            except ValueError:
                continue
            if isinstance(doc, dict) and doc.get("id") == want:
                return doc
        return None

    # ---------- protocol ----------
    def rpc(self, method: str, params=None, _retry: bool = True) -> dict:
        self._next_id += 1
        payload = {"jsonrpc": "2.0", "id": self._next_id, "method": method}
        if params is not None:
            payload["params"] = params
        sid = self.state.get("session_id")
        if not sid and method != "initialize" and _retry:
            self.initialize()
            return self.rpc(method, params, _retry=False)
        try:
            doc, new_sid = self._post(payload, sid)
        except RuntimeError as exc:
            text = str(exc).lower()
            expired = "session" in text or "http 404" in text or "http 400" in text
            if _retry and sid and expired:
                self.state.pop("session_id", None)
                self.save()
                self.initialize()
                return self.rpc(method, params, _retry=False)
            raise
        if new_sid:
            self.state["session_id"] = new_sid
            self.save()
        if doc is None:
            raise RuntimeError("empty JSON-RPC response for %s" % method)
        if "error" in doc:
            raise RpcError(doc)
        return doc.get("result", {})

    def notify(self, method: str, params=None) -> None:
        payload = {"jsonrpc": "2.0", "method": method, "params": params or {}}
        self._post(payload, self.state.get("session_id"))

    def initialize(self) -> dict:
        result = self.rpc(
            "initialize",
            {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": CLIENT_INFO,
            },
            _retry=False,
        )
        self.state["protocol_version"] = result.get("protocolVersion", PROTOCOL_VERSION)
        self.state["server_info"] = result.get("serverInfo", {})
        self.state["url"] = self.url
        self.save()
        self.notify("notifications/initialized")
        return result

def build_bridge(args) -> Bridge:
    state_path = getattr(args, "state", None) or os.environ.get("MCP_BRIDGE_STATE") or DEFAULT_STATE
    bridge = Bridge(
        url=os.environ.get("MCP_BRIDGE_URL", ""),
        state_path=state_path,
        timeout=getattr(args, "timeout", None) or 180.0,
    )
    url = getattr(args, "url", None) or os.environ.get("MCP_BRIDGE_URL") or bridge.state.get("url")
    if not url:
        sys.exit("no bridge URL: pass --url, set MCP_BRIDGE_URL, or write %s" % state_path)
    bridge.url = url
    return bridge


def load_args(spec, path) -> dict:
    if path:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    if spec in (None, ""):
        return {}
    if spec == "-":
        spec = sys.stdin.read()
    value = json.loads(spec)
    if not isinstance(value, dict):
        sys.exit("--args must be a JSON object")
    return value


def cmd_tools(bridge: Bridge, args) -> None:
    tools = bridge.rpc("tools/list", {}).get("tools", [])
    if args.json:
        payload = tools if args.verbose else [
            {"name": t.get("name"), "description": (t.get("description") or "").strip()}
            for t in tools
        ]
        print(json.dumps(payload, indent=2, ensure_ascii=False))
        return
    print("server: %s" % json.dumps(bridge.state.get("server_info", {}), ensure_ascii=False))
    print("tools: %d\n" % len(tools))
    for tool in tools:
        schema = tool.get("inputSchema") or {}
        required = schema.get("required") or []
        props = list((schema.get("properties") or {}).keys())
        lines = (tool.get("description") or "").strip().splitlines() or [""]
        head = "- %s" % tool.get("name")
        if required:
            head += "  [required: %s]" % ", ".join(required)
        print(head)
        print("    %s" % lines[0][:200])
        if args.verbose:
            print("    params: %s" % ", ".join(props))


def cmd_schema(bridge: Bridge, args) -> None:
    for tool in bridge.rpc("tools/list", {}).get("tools", []):
        if tool.get("name") == args.tool:
            print(json.dumps(tool, indent=2, ensure_ascii=False))
            return
    sys.exit("unknown tool: %s" % args.tool)


def unwrap(result):
    """Bridge tools return content[0].text holding a JSON string; decode it."""
    if not isinstance(result, dict):
        return result
    content = result.get("content")
    if not (isinstance(content, list) and content):
        return result
    texts = []
    for item in content:
        if not (isinstance(item, dict) and item.get("type") == "text"):
            return result
        texts.append(item.get("text") or "")
    if len(texts) != 1:
        return {"content": texts}
    try:
        return json.loads(texts[0])
    except ValueError:
        return texts[0]


def print_any(value) -> None:
    if isinstance(value, str):
        print(value)
    else:
        print(json.dumps(value, indent=2, ensure_ascii=False))


def cmd_call(bridge: Bridge, args) -> None:
    params = {"name": args.tool, "arguments": load_args(args.args, args.args_file)}
    result = bridge.rpc("tools/call", params)
    print(json.dumps(result, indent=2, ensure_ascii=False) if args.raw else "", end="")
    if not args.raw:
        print_any(unwrap(result))


def cmd_run(bridge: Bridge, args) -> None:
    arguments = {"command": args.command}
    if args.session:
        arguments["shell_session_id"] = args.session
    if args.request_id:
        arguments["request_id"] = args.request_id
    if args.background:
        arguments["background"] = True
    if args.timeout:
        arguments["timeout_seconds"] = args.timeout
    if args.wait:
        arguments["foreground_wait_seconds"] = args.wait
    if args.max_chars:
        arguments["max_output_characters"] = args.max_chars
    print_any(unwrap(bridge.rpc("tools/call", {"name": "run_command", "arguments": arguments})))


def cmd_out(bridge: Bridge, args) -> None:
    arguments = {"command_id": args.command_id}
    if args.offset:
        arguments["offset"] = args.offset
    if args.max_chars:
        arguments["max_output_characters"] = args.max_chars
    print_any(unwrap(bridge.rpc("tools/call", {"name": "get_command_output", "arguments": arguments})))

def main(argv) -> int:
    parser = argparse.ArgumentParser(description="Workspace MCP Bridge client")
    parser.add_argument("--url", help="MCP endpoint (streamable HTTP)")
    parser.add_argument("--state", help="state file path")
    parser.add_argument("--timeout", type=float, help="per-request timeout seconds")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("init")
    sub.add_parser("ping")
    sub.add_parser("reset")
    p_tools = sub.add_parser("tools")
    p_tools.add_argument("--json", action="store_true")
    p_tools.add_argument("--verbose", "-v", action="store_true")

    p_schema = sub.add_parser("schema")
    p_schema.add_argument("tool")

    p_call = sub.add_parser("call")
    p_call.add_argument("tool")
    p_call.add_argument("--args")
    p_call.add_argument("--args-file")
    p_call.add_argument("--raw", action="store_true", help="keep the MCP content envelope")

    p_run = sub.add_parser("run", help="run_command shortcut")
    p_run.add_argument("command")
    p_run.add_argument("--session", help="shell_session_id to reuse")
    p_run.add_argument("--request-id", help="idempotency key for side-effect commands")
    p_run.add_argument("--background", action="store_true")
    p_run.add_argument("--timeout", type=int, help="timeout_seconds (1-3600)")
    p_run.add_argument("--wait", type=int, help="foreground_wait_seconds (1-10)")
    p_run.add_argument("--max-chars", type=int, help="max_output_characters (2-32000)")

    p_out = sub.add_parser("out", help="get_command_output shortcut")
    p_out.add_argument("command_id")
    p_out.add_argument("--offset", type=int, default=0)
    p_out.add_argument("--max-chars", type=int)

    p_rpc = sub.add_parser("rpc")
    p_rpc.add_argument("method")
    p_rpc.add_argument("--params")

    args = parser.parse_args(argv)
    bridge = build_bridge(args)

    try:
        if args.cmd == "init":
            print(json.dumps(bridge.initialize(), indent=2, ensure_ascii=False))
            print("session_id: %s" % bridge.state.get("session_id"))
        elif args.cmd == "ping":
            print(json.dumps(bridge.rpc("ping", {}), indent=2, ensure_ascii=False))
        elif args.cmd == "reset":
            bridge.state.pop("session_id", None)
            bridge.save()
            print("session cleared")
        elif args.cmd == "tools":
            cmd_tools(bridge, args)
        elif args.cmd == "schema":
            cmd_schema(bridge, args)
        elif args.cmd == "call":
            cmd_call(bridge, args)
        elif args.cmd == "run":
            cmd_run(bridge, args)
        elif args.cmd == "out":
            cmd_out(bridge, args)
        elif args.cmd == "rpc":
            params = json.loads(args.params) if args.params else None
            print(json.dumps(bridge.rpc(args.method, params), indent=2, ensure_ascii=False))
    except RpcError as exc:
        print(json.dumps(exc.payload, indent=2, ensure_ascii=False), file=sys.stderr)
        return 1
    except RuntimeError as exc:
        print("error: %s" % exc, file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
