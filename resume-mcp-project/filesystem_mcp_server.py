
from __future__ import annotations

import argparse
import copy
import fnmatch
import hashlib
import json
import logging
import mimetypes
import os
import re
import sys
import threading
import time
import xml.etree.ElementTree as ET
import zipfile
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional

SERVER_NAME = "resume-filesystem-mcp"
SERVER_VERSION = "1.0.0"
PROTOCOL_VERSION = "2024-11-05"
SUPPORTED_PROTOCOLS = {"2024-11-05", "2025-03-26", "2025-06-18"}

# JSON-RPC 2.0 standard error codes
PARSE_ERROR = -32700
INVALID_REQUEST = -32600
METHOD_NOT_FOUND = -32601
INVALID_PARAMS = -32602
INTERNAL_ERROR = -32603
# MCP / server-defined
RESOURCE_NOT_FOUND = -32002
NOT_INITIALIZED = -32002

log = logging.getLogger(SERVER_NAME)


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #
class RPCError(Exception):
    """Protocol-level error -> JSON-RPC error object."""

    def __init__(self, code: int, message: str, data: Any = None):
        super().__init__(message)
        self.code, self.message, self.data = code, message, data

    def to_obj(self) -> dict:
        obj = {"code": self.code, "message": self.message}
        if self.data is not None:
            obj["data"] = self.data
        return obj


class ToolError(Exception):
    """Tool-level error -> result with isError=true."""


# --------------------------------------------------------------------------- #
# Generic, reusable JSON-RPC 2.0 / MCP server framework
# --------------------------------------------------------------------------- #
_JSON_TYPES = {
    "string": str, "integer": int, "number": (int, float),
    "boolean": bool, "array": list, "object": dict,
}


def validate_arguments(args: dict, schema: dict) -> None:
    """Minimal JSON-Schema check: required, type, enum."""
    for key in schema.get("required", []):
        if key not in args:
            raise RPCError(INVALID_PARAMS, f"Missing required argument: '{key}'")
    for key, val in args.items():
        spec = schema.get("properties", {}).get(key)
        if spec is None:
            continue  # extra args tolerated
        expected = _JSON_TYPES.get(spec.get("type"))
        if expected:
            bad = not isinstance(val, expected)
            if spec.get("type") in ("integer", "number") and isinstance(val, bool):
                bad = True
            if bad:
                raise RPCError(INVALID_PARAMS,
                               f"Argument '{key}' must be of type {spec['type']}")
        if "enum" in spec and val not in spec["enum"]:
            raise RPCError(INVALID_PARAMS,
                           f"Argument '{key}' must be one of {spec['enum']}")


class MCPServer:
    """Tiny MCP server: JSON-RPC 2.0 dispatcher + tool/resource registries."""

    def __init__(self, name: str, version: str, instructions: str = ""):
        self.name, self.version, self.instructions = name, version, instructions
        self.tools: Dict[str, dict] = {}
        self.resource_lister: Callable[[], List[dict]] = lambda: []
        self.resource_reader: Callable[[str], Optional[List[dict]]] = lambda uri: None
        self.resource_templates: List[dict] = []
        self.subscriptions: set = set()
        self.initialized = False
        self.on_shutdown: List[Callable[[], None]] = []
        self._out_lock = threading.Lock()
        self._writer = None  # set by serve_stdio

    # -- registration ------------------------------------------------------- #
    def tool(self, name: str, description: str, properties: dict,
             required: Optional[List[str]] = None):
        def deco(fn):
            self.tools[name] = {
                "schema": {
                    "name": name, "description": description,
                    "inputSchema": {"type": "object", "properties": properties,
                                    "required": required or []},
                },
                "fn": fn,
            }
            return fn
        return deco

    # -- output ------------------------------------------------------------- #
    def _write(self, obj: Any) -> None:
        if self._writer is None:
            return
        with self._out_lock:
            self._writer.write(json.dumps(obj, ensure_ascii=False) + "\n")
            self._writer.flush()

    def notify(self, method: str, params: Optional[dict] = None) -> None:
        msg = {"jsonrpc": "2.0", "method": method}
        if params is not None:
            msg["params"] = params
        self._write(msg)

    # -- JSON-RPC handling -------------------------------------------------- #
    def handle_raw(self, line: str) -> Optional[Any]:
        """Parse one line of input and return the response object (or None)."""
        try:
            obj = json.loads(line)
        except json.JSONDecodeError as exc:
            return self._error_response(None, RPCError(PARSE_ERROR, f"Parse error: {exc}"))
        return self.handle(obj)

    def handle(self, obj: Any) -> Optional[Any]:
        if isinstance(obj, list):  # JSON-RPC batch
            if not obj:
                return self._error_response(
                    None, RPCError(INVALID_REQUEST, "Empty batch"))
            responses = [r for r in (self._handle_one(o) for o in obj) if r is not None]
            return responses or None
        return self._handle_one(obj)

    def _handle_one(self, req: Any) -> Optional[dict]:
        req_id = req.get("id") if isinstance(req, dict) else None
        try:
            if (not isinstance(req, dict) or req.get("jsonrpc") != "2.0"
                    or not isinstance(req.get("method"), str)):
                raise RPCError(INVALID_REQUEST, "Invalid JSON-RPC 2.0 request")
            params = req.get("params", {})
            if params is None:
                params = {}
            if not isinstance(params, dict):
                raise RPCError(INVALID_PARAMS, "params must be an object")
            if "id" in req and not isinstance(req["id"], (str, int, type(None))):
                raise RPCError(INVALID_REQUEST, "id must be string, number or null")
            is_notification = "id" not in req
            result = self._dispatch(req["method"], params, is_notification)
            if is_notification:
                return None
            return {"jsonrpc": "2.0", "id": req["id"], "result": result}
        except RPCError as err:
            if isinstance(req, dict) and "id" not in req:
                log.debug("error on notification suppressed: %s", err.message)
                return None
            return self._error_response(req_id, err)
        except Exception as exc:  # noqa: BLE001
            log.exception("internal error")
            return self._error_response(req_id, RPCError(INTERNAL_ERROR, f"Internal error: {exc}"))

    @staticmethod
    def _error_response(req_id: Any, err: RPCError) -> dict:
        return {"jsonrpc": "2.0", "id": req_id, "error": err.to_obj()}

    # -- method dispatch ---------------------------------------------------- #
    def _dispatch(self, method: str, params: dict, is_notification: bool) -> Any:
        if method.startswith("notifications/"):
            if method == "notifications/initialized":
                self.initialized = True
            return None
        if method == "initialize":
            return self._m_initialize(params)
        if method == "ping":
            return {}
        if not self.initialized:
            raise RPCError(NOT_INITIALIZED, "Server not initialized: send 'initialize' first")
        table = {
            "tools/list": self._m_tools_list,
            "tools/call": self._m_tools_call,
            "resources/list": self._m_resources_list,
            "resources/templates/list": self._m_templates_list,
            "resources/read": self._m_resources_read,
            "resources/subscribe": self._m_subscribe,
            "resources/unsubscribe": self._m_unsubscribe,
            "prompts/list": lambda p: {"prompts": []},
        }
        fn = table.get(method)
        if fn is None:
            raise RPCError(METHOD_NOT_FOUND, f"Method not found: {method}")
        return fn(params)

    def _m_initialize(self, params: dict) -> dict:
        requested = params.get("protocolVersion")
        version = requested if requested in SUPPORTED_PROTOCOLS else PROTOCOL_VERSION
        # Some clients skip notifications/initialized; accept initialize as sufficient.
        self.initialized = True
        out = {
            "protocolVersion": version,
            "capabilities": {
                "tools": {"listChanged": False},
                "resources": {"subscribe": True, "listChanged": True},
            },
            "serverInfo": {"name": self.name, "version": self.version},
        }
        if self.instructions:
            out["instructions"] = self.instructions
        return out

    def _m_tools_list(self, params: dict) -> dict:
        return {"tools": [t["schema"] for t in self.tools.values()]}

    def _m_tools_call(self, params: dict) -> dict:
        name = params.get("name")
        if not isinstance(name, str):
            raise RPCError(INVALID_PARAMS, "'name' is required")
        tool = self.tools.get(name)
        if tool is None:
            raise RPCError(INVALID_PARAMS, f"Unknown tool: {name}")
        args = params.get("arguments") or {}
        if not isinstance(args, dict):
            raise RPCError(INVALID_PARAMS, "'arguments' must be an object")
        validate_arguments(args, tool["schema"]["inputSchema"])
        try:
            result = tool["fn"](**args)
            text = json.dumps(result, ensure_ascii=False)
            out = {"content": [{"type": "text", "text": text}], "isError": False}
            if isinstance(result, dict):
                out["structuredContent"] = result
            return out
        except ToolError as exc:
            return {"content": [{"type": "text", "text": str(exc)}], "isError": True}
        except TypeError as exc:  # bad kwargs
            raise RPCError(INVALID_PARAMS, f"Invalid arguments for {name}: {exc}")
        except Exception as exc:  # noqa: BLE001
            log.exception("tool %s crashed", name)
            return {"content": [{"type": "text", "text": f"Tool crashed: {exc}"}], "isError": True}

    def _m_resources_list(self, params: dict) -> dict:
        return {"resources": self.resource_lister()}

    def _m_templates_list(self, params: dict) -> dict:
        return {"resourceTemplates": self.resource_templates}

    def _m_resources_read(self, params: dict) -> dict:
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise RPCError(INVALID_PARAMS, "'uri' is required")
        contents = self.resource_reader(uri)
        if contents is None:
            raise RPCError(RESOURCE_NOT_FOUND, "Resource not found", {"uri": uri})
        return {"contents": contents}

    def _m_subscribe(self, params: dict) -> dict:
        uri = params.get("uri")
        if not isinstance(uri, str):
            raise RPCError(INVALID_PARAMS, "'uri' is required")
        self.subscriptions.add(uri)
        return {}

    def _m_unsubscribe(self, params: dict) -> dict:
        self.subscriptions.discard(params.get("uri"))
        return {}

    # -- transport ---------------------------------------------------------- #
    def serve_stdio(self, stdin=None, stdout=None) -> None:
        stdin = stdin or sys.stdin
        self._writer = stdout or sys.stdout
        log.info("%s %s listening on stdio", self.name, self.version)
        try:
            for line in stdin:
                line = line.strip()
                if not line:
                    continue
                resp = self.handle_raw(line)
                if resp is not None:
                    self._write(resp)
        finally:
            for fn in self.on_shutdown:
                try:
                    fn()
                except Exception:  # noqa: BLE001
                    pass


# --------------------------------------------------------------------------- #
# Configuration management
# --------------------------------------------------------------------------- #
DEFAULT_CONFIG: Dict[str, Any] = {
    "root_dir": "./data",
    "allowed_extensions": [".txt", ".md", ".pdf", ".docx", ".json"],
    "writable_extensions": [".txt", ".md", ".json"],
    "max_file_size_mb": 10,
    "max_read_chars": 200_000,
    "batch_workers": 4,
    "max_batch_size": 200,
    "watch_interval_seconds": 2.0,
}
RUNTIME_MUTABLE = {
    "allowed_extensions": list, "max_file_size_mb": (int, float),
    "batch_workers": int, "max_batch_size": int, "watch_interval_seconds": (int, float),
}


def load_config(config_path: Optional[str], root_override: Optional[str]) -> Dict[str, Any]:
    """Precedence: defaults < config file < RESUME_MCP_ROOT env < --root flag."""
    cfg = copy.deepcopy(DEFAULT_CONFIG)
    if config_path:
        with open(config_path, encoding="utf-8") as fh:
            cfg.update(json.load(fh))
    if os.environ.get("RESUME_MCP_ROOT"):
        cfg["root_dir"] = os.environ["RESUME_MCP_ROOT"]
    if root_override:
        cfg["root_dir"] = root_override
    cfg["root_dir"] = str(Path(cfg["root_dir"]).expanduser().resolve())
    return cfg


# --------------------------------------------------------------------------- #
# Resume text extraction / parsing helpers
# --------------------------------------------------------------------------- #
EMAIL_RE = re.compile(r"[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}")
PHONE_RE = re.compile(r"\+?\d[\d\s().-]{7,}\d")
YEARS_RE = re.compile(r"(\d{1,2})\+?\s*(?:years|yrs)", re.I)
RANGE_RE = re.compile(r"((?:19|20)\d{2})\s*[-–—to]+\s*((?:19|20)\d{2}|present|current|now)", re.I)
SECTION_NAMES = {"summary", "profile", "objective", "experience", "work experience",
                 "education", "skills", "technical skills", "projects",
                 "certifications", "awards", "publications"}


def _docx_text(path: Path) -> str:
    with zipfile.ZipFile(path) as zf:
        xml = zf.read("word/document.xml")
    ns = "{http://schemas.openxmlformats.org/wordprocessingml/2006/main}"
    root = ET.fromstring(xml)
    paras = []
    for p in root.iter(f"{ns}p"):
        paras.append("".join(t.text or "" for t in p.iter(f"{ns}t")))
    return "\n".join(paras)


def _pdf_text(path: Path) -> str:
    try:
        from pypdf import PdfReader
    except ImportError as exc:
        raise ToolError("PDF support requires 'pip install pypdf'") from exc
    reader = PdfReader(str(path))
    return "\n".join((page.extract_text() or "") for page in reader.pages)


def extract_text(path: Path) -> str:
    ext = path.suffix.lower()
    if ext == ".docx":
        return _docx_text(path)
    if ext == ".pdf":
        return _pdf_text(path)
    return path.read_text(encoding="utf-8", errors="replace")


def parse_resume_text(text: str) -> dict:
    lines = [ln.strip() for ln in text.splitlines()]
    nonempty = [ln for ln in lines if ln]
    sections = [ln.rstrip(":") for ln in lines if ln.rstrip(":").lower() in SECTION_NAMES]
    stated = [int(m) for m in YEARS_RE.findall(text) if 0 < int(m) <= 50]
    now = datetime.now().year
    spans = []
    for a, b in RANGE_RE.findall(text):
        end = now if b.lower() in ("present", "current", "now") else int(b)
        if int(a) <= end:
            spans.append((int(a), end))
    spans.sort()
    merged: List[List[int]] = []
    for s, e in spans:
        if merged and s <= merged[-1][1]:
            merged[-1][1] = max(merged[-1][1], e)
        else:
            merged.append([s, e])
    dated = sum(e - s for s, e in merged)
    return {
        "name_guess": nonempty[0] if nonempty else "",
        "emails": sorted(set(EMAIL_RE.findall(text))),
        "phones": sorted({p.strip() for p in PHONE_RE.findall(text) if 9 <= len(re.sub(r"\D", "", p)) <= 15}),
        "sections": sections,
        "years_experience": max(stated + [dated]) if (stated or dated) else 0,
        "word_count": len(text.split()),
    }


def _iso(ts: float) -> str:
    return datetime.fromtimestamp(ts, tz=timezone.utc).isoformat(timespec="seconds")


# --------------------------------------------------------------------------- #
# Directory watcher (polling; portable, no external deps)
# --------------------------------------------------------------------------- #
class DirectoryWatcher:
    def __init__(self, service: "FileSystemService", watch_id: str, rel_dir: str,
                 interval: float, recursive: bool, on_event: Callable[[dict], None]):
        self.service, self.watch_id, self.rel_dir = service, watch_id, rel_dir
        self.interval, self.recursive, self.on_event = interval, recursive, on_event
        self.events: List[dict] = []
        self.lock = threading.Lock()
        self.stop_flag = threading.Event()
        self.known = self._snapshot()
        self.started = time.time()
        self.thread = threading.Thread(target=self._run, name=watch_id, daemon=True)

    def _snapshot(self) -> Dict[str, tuple]:
        snap = {}
        base = self.service.resolve(self.rel_dir)
        it = base.rglob("*") if self.recursive else base.glob("*")
        for p in it:
            if p.is_file() and p.suffix.lower() in self.service.cfg["allowed_extensions"]:
                st = p.stat()
                snap[self.service.rel(p)] = (st.st_mtime_ns, st.st_size)
        return snap

    def scan(self) -> List[dict]:
        """Diff against the last snapshot; record and return new events."""
        with self.lock:
            current = self._snapshot()
            new_events = []
            for rel, sig in current.items():
                if rel not in self.known:
                    new_events.append({"event": "created", "path": rel})
                elif self.known[rel] != sig:
                    new_events.append({"event": "modified", "path": rel})
            for rel in self.known:
                if rel not in current:
                    new_events.append({"event": "deleted", "path": rel})
            self.known = current
            ts = _iso(time.time())
            for ev in new_events:
                ev.update(watch_id=self.watch_id, detected_at=ts,
                          uri=f"resume:///{ev['path']}")
            self.events.extend(new_events)
        for ev in new_events:
            try:
                self.on_event(ev)
            except Exception:  # noqa: BLE001
                log.exception("watch callback failed")
        return new_events

    def _run(self):
        while not self.stop_flag.wait(self.interval):
            try:
                self.scan()
            except Exception:  # noqa: BLE001
                log.exception("watcher scan failed")

    def drain(self) -> List[dict]:
        with self.lock:
            out, self.events = self.events, []
        return out


# --------------------------------------------------------------------------- #
# The file-system service (all tool implementations)
# --------------------------------------------------------------------------- #
class FileSystemService:
    def __init__(self, cfg: Dict[str, Any], server: MCPServer):
        self.cfg, self.server = cfg, server
        self.root = Path(cfg["root_dir"])
        self.root.mkdir(parents=True, exist_ok=True)
        self.watchers: Dict[str, DirectoryWatcher] = {}
        self._watch_seq = 0
        self._watch_lock = threading.Lock()

    # -- path safety -------------------------------------------------------- #
    def resolve(self, rel: str = "") -> Path:
        rel = (rel or "").lstrip("/\\")
        p = (self.root / rel).resolve()
        if p != self.root and self.root not in p.parents:
            raise ToolError(f"Path escapes the sandbox root: {rel}")
        return p

    def rel(self, p: Path) -> str:
        return p.relative_to(self.root).as_posix()

    def _check_file(self, rel: str) -> Path:
        p = self.resolve(rel)
        if not p.exists():
            raise ToolError(f"File not found: {rel}")
        if not p.is_file():
            raise ToolError(f"Not a file: {rel}")
        if p.suffix.lower() not in self.cfg["allowed_extensions"]:
            raise ToolError(f"Extension '{p.suffix}' not allowed")
        if p.stat().st_size > self.cfg["max_file_size_mb"] * 1024 * 1024:
            raise ToolError(f"File exceeds {self.cfg['max_file_size_mb']} MB limit")
        return p

    # -- Milestone 1 tools -------------------------------------------------- #
    def list_files(self, directory: str = "", recursive: bool = True, pattern: str = "*") -> dict:
        base = self.resolve(directory)
        if not base.is_dir():
            raise ToolError(f"Directory not found: {directory or '.'}")
        it = base.rglob("*") if recursive else base.glob("*")
        files = []
        for p in sorted(it):
            if (p.is_file() and p.suffix.lower() in self.cfg["allowed_extensions"]
                    and fnmatch.fnmatch(p.name, pattern)):
                st = p.stat()
                files.append({"path": self.rel(p), "name": p.name, "extension": p.suffix.lower(),
                              "size": st.st_size, "modified": _iso(st.st_mtime)})
        return {"directory": directory or ".", "count": len(files), "files": files}

    def read_file(self, path: str, max_chars: Optional[int] = None) -> dict:
        p = self._check_file(path)
        text = extract_text(p)
        limit = max_chars or self.cfg["max_read_chars"]
        truncated = len(text) > limit
        return {"path": path, "text": text[:limit], "truncated": truncated, "chars": len(text)}

    def get_file_info(self, path: str) -> dict:
        p = self._check_file(path)
        st = p.stat()
        return {"path": path, "size": st.st_size, "extension": p.suffix.lower(),
                "mime_type": mimetypes.guess_type(p.name)[0] or "application/octet-stream",
                "modified": _iso(st.st_mtime), "created": _iso(st.st_ctime),
                "sha256": hashlib.sha256(p.read_bytes()).hexdigest()}

    def search_files(self, query: str, directory: str = "", case_sensitive: bool = False,
                     regex: bool = False, max_results: int = 50) -> dict:
        flags = 0 if case_sensitive else re.IGNORECASE
        try:
            pat = re.compile(query if regex else re.escape(query), flags)
        except re.error as exc:
            raise ToolError(f"Invalid regex: {exc}")
        matches = []
        for f in self.list_files(directory)["files"]:
            try:
                text = extract_text(self.resolve(f["path"]))
            except Exception:  # noqa: BLE001
                continue
            hits = list(pat.finditer(text))
            if hits:
                m = hits[0]
                snippet = text[max(0, m.start() - 40): m.end() + 40].replace("\n", " ")
                matches.append({"path": f["path"], "hit_count": len(hits), "snippet": snippet})
            if len(matches) >= max_results:
                break
        return {"query": query, "count": len(matches), "matches": matches}

    def parse_resume(self, path: str, include_text: bool = False) -> dict:
        text = self.read_file(path)["text"]
        data = {"path": path, **parse_resume_text(text)}
        if include_text:
            data["text"] = text
        return data

    def write_file(self, path: str, content: str, overwrite: bool = True) -> dict:
        p = self.resolve(path)
        if p.suffix.lower() not in self.cfg["writable_extensions"]:
            raise ToolError(f"Writing '{p.suffix}' files is not allowed")
        if p.exists() and not overwrite:
            raise ToolError(f"File exists and overwrite=false: {path}")
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(p.suffix + ".tmp")
        tmp.write_text(content, encoding="utf-8")
        tmp.replace(p)
        return {"path": path, "bytes_written": len(content.encode("utf-8"))}

    # -- NEW: batch_process ------------------------------------------------- #
    def batch_process(self, paths: Optional[List[str]] = None, directory: Optional[str] = None,
                      pattern: str = "*", operation: str = "parse",
                      max_workers: Optional[int] = None, include_text: bool = True) -> dict:
        if not paths and directory is None:
            raise ToolError("Provide 'paths' or 'directory'")
        if not paths:
            paths = [f["path"] for f in self.list_files(directory, True, pattern)["files"]]
        if len(paths) > self.cfg["max_batch_size"]:
            raise ToolError(f"Batch too large ({len(paths)} > {self.cfg['max_batch_size']})")
        workers = max(1, min(max_workers or self.cfg["batch_workers"], 16))

        def one(rel: str) -> dict:
            t0 = time.perf_counter()
            try:
                if operation == "parse":
                    data = self.parse_resume(rel, include_text=include_text)
                elif operation == "extract_text":
                    data = self.read_file(rel)
                elif operation == "info":
                    data = self.get_file_info(rel)
                else:
                    raise ToolError(f"Unknown operation: {operation}")
                status, payload = "ok", {"data": data}
            except ToolError as exc:
                status, payload = "error", {"error": str(exc)}
            except Exception as exc:  # noqa: BLE001 - never let one file kill the batch
                status, payload = "error", {"error": f"{type(exc).__name__}: {exc}"}
            return {"path": rel, "status": status,
                    "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2), **payload}

        t0 = time.perf_counter()
        with ThreadPoolExecutor(max_workers=workers) as pool:
            results = list(pool.map(one, paths))  # preserves input order
        ok = sum(r["status"] == "ok" for r in results)
        return {"operation": operation, "total": len(results), "succeeded": ok,
                "failed": len(results) - ok, "workers": workers,
                "elapsed_ms": round((time.perf_counter() - t0) * 1000, 2), "results": results}

    # -- NEW: watch_directory ----------------------------------------------- #
    def _on_watch_event(self, ev: dict) -> None:
        if ev["event"] in ("created", "deleted"):
            self.server.notify("notifications/resources/list_changed")
        if ev["event"] in ("modified", "created") and ev["uri"] in self.server.subscriptions:
            self.server.notify("notifications/resources/updated", {"uri": ev["uri"]})
        self.server.notify("notifications/message", {
            "level": "info", "logger": "watch_directory", "data": ev})

    def watch_directory(self, action: str, directory: str = "", watch_id: Optional[str] = None,
                        interval_seconds: Optional[float] = None, recursive: bool = True) -> dict:
        with self._watch_lock:
            if action == "start":
                if not self.resolve(directory).is_dir():
                    raise ToolError(f"Directory not found: {directory or '.'}")
                for w in self.watchers.values():  # idempotent per (dir, recursive)
                    if w.rel_dir == directory and w.recursive == recursive:
                        return {"watch_id": w.watch_id, "status": "already_running",
                                "directory": directory or ".", "tracked_files": len(w.known)}
                interval = float(interval_seconds or self.cfg["watch_interval_seconds"])
                if interval < 0.05:
                    raise ToolError("interval_seconds must be >= 0.05")
                self._watch_seq += 1
                wid = f"watch-{self._watch_seq}"
                w = DirectoryWatcher(self, wid, directory, interval, recursive, self._on_watch_event)
                self.watchers[wid] = w
                w.thread.start()
                return {"watch_id": wid, "status": "started", "directory": directory or ".",
                        "interval_seconds": interval, "tracked_files": len(w.known)}
            if action == "list":
                return {"watchers": [{"watch_id": w.watch_id, "directory": w.rel_dir or ".",
                                      "tracked_files": len(w.known), "pending_events": len(w.events)}
                                     for w in self.watchers.values()]}
            w = self.watchers.get(watch_id or "")
            if w is None:
                raise ToolError(f"Unknown watch_id: {watch_id}")
            if action == "poll":
                w.scan()  # force an immediate diff so callers never wait for the timer
                events = w.drain()
                return {"watch_id": w.watch_id, "count": len(events), "events": events}
            if action == "stop":
                w.stop_flag.set()
                del self.watchers[w.watch_id]
                return {"watch_id": w.watch_id, "status": "stopped"}
            raise ToolError(f"Unknown action: {action}")

    def stop_all_watchers(self) -> None:
        for w in list(self.watchers.values()):
            w.stop_flag.set()
        self.watchers.clear()

    # -- configuration management ------------------------------------------- #
    def get_config(self) -> dict:
        return dict(self.cfg)

    def update_config(self, key: str, value: Any) -> dict:
        if key not in RUNTIME_MUTABLE:
            raise ToolError(f"'{key}' is not runtime-configurable "
                            f"(allowed: {sorted(RUNTIME_MUTABLE)})")
        expected = RUNTIME_MUTABLE[key]
        if isinstance(value, bool) or not isinstance(value, expected):
            raise ToolError(f"'{key}' has wrong type")
        if key == "allowed_extensions":
            value = [v.lower() if v.startswith(".") else "." + v.lower() for v in value]
        elif value <= 0:
            raise ToolError(f"'{key}' must be positive")
        self.cfg[key] = value
        return {"updated": key, "value": value}

    # -- resources ---------------------------------------------------------- #
    def list_resources(self) -> List[dict]:
        out = [{"uri": "config://server", "name": "Server configuration",
                "description": "Current effective configuration", "mimeType": "application/json"}]
        for f in self.list_files()["files"][:1000]:
            out.append({"uri": f"resume:///{f['path']}", "name": f["path"],
                        "description": f"{f['size']} bytes, modified {f['modified']}",
                        "mimeType": mimetypes.guess_type(f["name"])[0] or "text/plain"})
        return out

    def read_resource(self, uri: str) -> Optional[List[dict]]:
        if uri == "config://server":
            return [{"uri": uri, "mimeType": "application/json",
                     "text": json.dumps(self.cfg, indent=2)}]
        if uri.startswith("resume:///"):
            try:
                text = self.read_file(uri[len("resume:///"):])["text"]
            except ToolError:
                return None
            return [{"uri": uri, "mimeType": "text/plain", "text": text}]
        return None


# --------------------------------------------------------------------------- #
# Wiring
# --------------------------------------------------------------------------- #
def build_server(cfg: Dict[str, Any]) -> MCPServer:
    server = MCPServer(
        SERVER_NAME, SERVER_VERSION,
        instructions="Resume file-system tools. Paths are relative to the sandbox root. "
                     "Use batch_process for many files and watch_directory to detect new resumes.")
    svc = FileSystemService(cfg, server)
    server.service = svc  # exposed for tests
    server.resource_lister = svc.list_resources
    server.resource_reader = svc.read_resource
    server.resource_templates = [{
        "uriTemplate": "resume:///{path}", "name": "Resume / job file",
        "description": "Any allowed file under the sandbox root, as extracted text",
        "mimeType": "text/plain"}]
    server.on_shutdown.append(svc.stop_all_watchers)

    S, I, B = {"type": "string"}, {"type": "integer"}, {"type": "boolean"}
    server.tool("list_files", "List resume/job files under a directory.",
                {"directory": S, "recursive": B, "pattern": S})(svc.list_files)
    server.tool("read_file", "Read a file as text (txt/md/pdf/docx/json).",
                {"path": S, "max_chars": I}, ["path"])(svc.read_file)
    server.tool("get_file_info", "File size, type, timestamps and SHA-256.",
                {"path": S}, ["path"])(svc.get_file_info)
    server.tool("search_files", "Search file contents (substring or regex).",
                {"query": S, "directory": S, "case_sensitive": B, "regex": B, "max_results": I},
                ["query"])(svc.search_files)
    server.tool("parse_resume", "Extract contact info, sections and years of experience.",
                {"path": S, "include_text": B}, ["path"])(svc.parse_resume)
    server.tool("write_file", "Write a .txt/.md/.json file (e.g. a ranking report).",
                {"path": S, "content": S, "overwrite": B}, ["path", "content"])(svc.write_file)
    server.tool("batch_process",
                "Process many files concurrently. Give 'paths' or 'directory'. "
                "operation: parse | extract_text | info.",
                {"paths": {"type": "array", "items": S}, "directory": S, "pattern": S,
                 "operation": {"type": "string", "enum": ["parse", "extract_text", "info"]},
                 "max_workers": I, "include_text": B})(svc.batch_process)
    server.tool("watch_directory",
                "Monitor a directory for new/modified/deleted resumes. "
                "action: start | poll | stop | list.",
                {"action": {"type": "string", "enum": ["start", "poll", "stop", "list"]},
                 "directory": S, "watch_id": S,
                 "interval_seconds": {"type": "number"}, "recursive": B},
                ["action"])(svc.watch_directory)
    server.tool("get_config", "Return the effective server configuration.", {})(svc.get_config)
    server.tool("update_config", "Change a runtime-mutable config value.",
                {"key": S, "value": {}}, ["key", "value"])(svc.update_config)
    return server


def main(argv: Optional[List[str]] = None) -> None:
    ap = argparse.ArgumentParser(description="Resume filesystem MCP server (stdio)")
    ap.add_argument("--root", help="sandbox root directory")
    ap.add_argument("--config", help="path to JSON config file")
    ap.add_argument("--log-level", default="WARNING")
    args = ap.parse_args(argv)
    logging.basicConfig(stream=sys.stderr, level=args.log_level.upper(),
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    server = build_server(load_config(args.config, args.root))
    server.serve_stdio()


if __name__ == "__main__":
    main()
