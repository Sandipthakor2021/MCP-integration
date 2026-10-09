
from __future__ import annotations

import json
import queue
import subprocess
import sys
import threading
from typing import Any, Dict, List, Optional


class MCPError(Exception):
    """JSON-RPC protocol error returned by the server."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(f"[{code}] {message}")
        self.code, self.message, self.data = code, message, data


class MCPToolError(Exception):
    """Tool executed but reported isError=true."""


class MCPClient:
    def __init__(self, name: str, command: List[str], env: Optional[dict] = None,
                 timeout: float = 30.0, trace: bool = False):
        self.name, self.command, self.env = name, command, env
        self.timeout, self.trace = timeout, trace
        self.proc: Optional[subprocess.Popen] = None
        self._next_id = 0
        self._id_lock = threading.Lock()
        self._pending: Dict[int, "queue.Queue"] = {}
        self.notifications: "queue.Queue[dict]" = queue.Queue()
        self.call_log: List[dict] = []
        self.server_info: dict = {}
        self.capabilities: dict = {}
        self._reader: Optional[threading.Thread] = None

    # -- lifecycle ----------------------------------------------------------- #
    def start(self) -> dict:
        self.proc = subprocess.Popen(
            self.command, stdin=subprocess.PIPE, stdout=subprocess.PIPE,
            stderr=sys.stderr, text=True, bufsize=1, env=self.env)
        self._reader = threading.Thread(target=self._read_loop, daemon=True)
        self._reader.start()
        res = self.request("initialize", {
            "protocolVersion": "2024-11-05", "capabilities": {},
            "clientInfo": {"name": "resume-matching-agent", "version": "1.0.0"}})
        self.server_info, self.capabilities = res.get("serverInfo", {}), res.get("capabilities", {})
        self.notify("notifications/initialized")
        return res

    def close(self) -> None:
        if self.proc and self.proc.poll() is None:
            try:
                self.proc.stdin.close()
                self.proc.wait(timeout=3)
            except Exception:  # noqa: BLE001
                self.proc.kill()

    def __enter__(self):
        self.start()
        return self

    def __exit__(self, *exc):
        self.close()

    # -- transport ----------------------------------------------------------- #
    def _read_loop(self) -> None:
        for line in self.proc.stdout:
            line = line.strip()
            if not line:
                continue
            try:
                msg = json.loads(line)
            except json.JSONDecodeError:
                continue
            if self.trace:
                print(f"  <-- [{self.name}] {line[:300]}")
            if "id" in msg and ("result" in msg or "error" in msg):
                q = self._pending.pop(msg["id"], None)
                if q:
                    q.put(msg)
            else:
                self.notifications.put(msg)
        # server exited: fail any waiting callers
        for q in list(self._pending.values()):
            q.put({"error": {"code": -32000, "message": "Server closed connection"}})

    def _send(self, obj: dict) -> None:
        line = json.dumps(obj)
        if self.trace:
            print(f"  --> [{self.name}] {line[:300]}")
        self.proc.stdin.write(line + "\n")
        self.proc.stdin.flush()

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)

    def request(self, method: str, params: Optional[dict] = None) -> dict:
        with self._id_lock:
            self._next_id += 1
            rid = self._next_id
        q: "queue.Queue" = queue.Queue(maxsize=1)
        self._pending[rid] = q
        msg = {"jsonrpc": "2.0", "id": rid, "method": method}
        if params is not None:
            msg["params"] = params
        self._send(msg)
        try:
            resp = q.get(timeout=self.timeout)
        except queue.Empty:
            self._pending.pop(rid, None)
            raise MCPError(-32000, f"Timeout waiting for '{method}' from {self.name}")
        if "error" in resp:
            e = resp["error"]
            raise MCPError(e.get("code", -32000), e.get("message", ""), e.get("data"))
        return resp["result"]

    # -- MCP helpers --------------------------------------------------------- #
    def list_tools(self) -> List[dict]:
        return self.request("tools/list")["tools"]

    def list_resources(self) -> List[dict]:
        return self.request("resources/list")["resources"]

    def list_resource_templates(self) -> List[dict]:
        return self.request("resources/templates/list")["resourceTemplates"]

    def read_resource(self, uri: str) -> str:
        contents = self.request("resources/read", {"uri": uri})["contents"]
        return contents[0].get("text", "")

    def subscribe(self, uri: str) -> None:
        self.request("resources/subscribe", {"uri": uri})

    def call_tool(self, name: str, arguments: Optional[dict] = None) -> Any:
        """Call a tool; returns parsed JSON (or raw text). Raises MCPToolError on isError."""
        res = self.request("tools/call", {"name": name, "arguments": arguments or {}})
        text = "".join(c.get("text", "") for c in res.get("content", []) if c.get("type") == "text")
        self.call_log.append({"server": self.name, "tool": name, "args": arguments or {},
                              "isError": res.get("isError", False)})
        if res.get("isError"):
            raise MCPToolError(text)
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            return text

    def drain_notifications(self) -> List[dict]:
        out = []
        while True:
            try:
                out.append(self.notifications.get_nowait())
            except queue.Empty:
                return out
