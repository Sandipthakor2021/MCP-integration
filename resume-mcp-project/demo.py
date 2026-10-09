
import argparse
import json
import shutil
import sys
import tempfile
import threading
import time
from pathlib import Path

import matching_agent as agent
from mcp_client import MCPClient, MCPError

HERE = Path(__file__).resolve().parent


def scene(n, title, pause):
    time.sleep(pause)
    print(f"\n{'=' * 78}\nSCENE {n}: {title}\n{'=' * 78}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--pause", type=float, default=0.0)
    p = ap.parse_args().pause
    tmp = Path(tempfile.mkdtemp(prefix="mcp_demo_"))
    root = tmp / "data"
    shutil.copytree(HERE / "data", root)
    server_cmd = [sys.executable, str(HERE / "filesystem_mcp_server.py"), "--root", str(root)]

    try:
        scene(1, "JSON-RPC 2.0 handshake and discovery (raw traffic)", p)
        with MCPClient("filesystem", server_cmd, trace=True) as c:
            print("server:", c.server_info, "| capabilities:", json.dumps(c.capabilities))
            tools = c.list_tools()
            print("TOOLS:", ", ".join(t["name"] for t in tools))

            scene(2, "Resource discovery: resources/list, templates, read, subscribe", p)
            c.trace = False
            for r in c.list_resources():
                print("  resource:", r["uri"])
            print("  template:", c.list_resource_templates()[0]["uriTemplate"])
            print("  read    :", c.read_resource("resume:///resumes/bob_smith.txt").splitlines()[0], "...")

            scene(3, "Error handling: protocol errors vs tool errors vs sandbox", p)
            for label, fn in [
                ("unknown method", lambda: c.request("resources/frobnicate")),
                ("missing resource", lambda: c.read_resource("resume:///ghost.txt")),
                ("bad arguments", lambda: c.request("tools/call", {"name": "read_file", "arguments": {}})),
            ]:
                try:
                    fn()
                except MCPError as e:
                    print(f"  {label:17s} -> JSON-RPC error {e.code}: {e.message}")
            for label, args in [("path traversal", {"path": "../../etc/passwd"}),
                                ("missing file", {"path": "resumes/ghost.txt"})]:
                try:
                    c.call_tool("read_file", args)
                except Exception as e:  # MCPToolError
                    print(f"  {label:17s} -> tool result isError=true: {e}")

            scene(4, "NEW capability: batch_process (thread pool, partial failures tolerated)", p)
            out = c.call_tool("batch_process", {"paths": [
                "resumes/alice_johnson.txt", "resumes/bob_smith.txt", "resumes/carol_diaz.txt",
                "resumes/dan_wright.txt", "resumes/ghost.txt"], "include_text": False})
            print(f"  total={out['total']} ok={out['succeeded']} failed={out['failed']} "
                  f"workers={out['workers']} elapsed={out['elapsed_ms']} ms")
            for r in out["results"]:
                extra = (f"years={r['data']['years_experience']}" if r["status"] == "ok" else r["error"])
                print(f"    {r['status']:5s} {r['path']:30s} {extra}")

            scene(5, "NEW capability: watch_directory (+ server notifications)", p)
            w = c.call_tool("watch_directory", {"action": "start", "directory": "resumes",
                                                "interval_seconds": 0.2})
            print("  started:", w)
            (root / "resumes" / "grace_lee.txt").write_text(
                "Grace Lee\ngrace@example.com\nSUMMARY\n7 years of experience\nSKILLS\n"
                "Python, FastAPI, REST, Postgres, Docker, AWS, Git, Kubernetes, Terraform\n")
            print("  ...dropped resumes/grace_lee.txt into the folder")
            time.sleep(0.8)
            for n in c.drain_notifications():
                print("  server notification:", n["method"])
            print("  poll:", c.call_tool("watch_directory", {"action": "poll", "watch_id": w["watch_id"]})["events"])
            c.call_tool("watch_directory", {"action": "stop", "watch_id": w["watch_id"]})

            scene(6, "Configuration management", p)
            print("  workers before:", c.call_tool("get_config")["batch_workers"])
            print("  update:", c.call_tool("update_config", {"key": "batch_workers", "value": 8}))
            try:
                c.call_tool("update_config", {"key": "root_dir", "value": "/"})
            except Exception as e:
                print("  root_dir change rejected:", e)

        scene(7, "LangGraph agent using TWO MCP servers (filesystem + skills-db)", p)
        reg = agent.MCPRegistry(agent.default_servers(str(root)))
        try:
            st = agent.run_matching(reg)
            for name, info in st["discovery"].items():
                print(f"  connected {name}: tools={info['tools']}")
            print("  graph path:", " -> ".join(st["steps"]))
            print()
            print(st["report"])
            print("  MCP calls made by the agent (agent has NO direct file access):")
            counts = {}
            for e in reg.call_log:
                counts[(e["server"], e["tool"])] = counts.get((e["server"], e["tool"]), 0) + 1
            for (srv, tool), n in counts.items():
                print(f"    {srv:10s} {tool:15s} x{n}")

            scene(8, "Agent + watch_directory: a new resume arrives while the agent runs", p)
            def drop():
                time.sleep(1.5)
                (root / "resumes" / "heidi_park.txt").write_text(
                    "Heidi Park\nheidi@example.com\nSKILLS\nPython, Docker, AWS, Git, REST APIs, "
                    "PostgreSQL\n5 years of experience")
            t = threading.Thread(target=drop); t.start()
            res = agent.watch_and_match(reg, seconds=5, interval=0.5)
            t.join()
            for st in res:
                print(st["report"])
        finally:
            reg.close()
        print("\nDemo complete.")
    finally:
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    main()
