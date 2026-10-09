
import json
import shutil
import sys
import tempfile
import time
import unittest
import zipfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import filesystem_mcp_server as fs
from mcp_client import MCPClient, MCPError, MCPToolError

PROJECT = Path(__file__).resolve().parents[1]
SERVER = str(PROJECT / "filesystem_mcp_server.py")


def make_root() -> Path:
    tmp = Path(tempfile.mkdtemp(prefix="mcp_test_"))
    shutil.copytree(PROJECT / "data", tmp / "data")
    return tmp / "data"


def rpc(server, method, params=None, id=1):
    msg = {"jsonrpc": "2.0", "id": id, "method": method}
    if params is not None:
        msg["params"] = params
    return server.handle(msg)


class InProcessBase(unittest.TestCase):
    def setUp(self):
        self.root = make_root()
        cfg = fs.load_config(None, str(self.root))
        self.server = fs.build_server(cfg)
        rpc(self.server, "initialize", {"protocolVersion": "2024-11-05"})

    def tearDown(self):
        self.server.service.stop_all_watchers()
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def call(self, name, **args):
        return rpc(self.server, "tools/call", {"name": name, "arguments": args})


class TestProtocol(InProcessBase):
    def test_s1_initialize_handshake(self):
        r = rpc(self.server, "initialize", {"protocolVersion": "2024-11-05"})
        self.assertEqual(r["jsonrpc"], "2.0")
        self.assertEqual(r["id"], 1)
        res = r["result"]
        self.assertIn("tools", res["capabilities"])
        self.assertTrue(res["capabilities"]["resources"]["subscribe"])
        self.assertEqual(res["serverInfo"]["name"], fs.SERVER_NAME)

    def test_s2_error_codes(self):
        self.assertEqual(self.server.handle_raw("{not json")["error"]["code"], fs.PARSE_ERROR)
        self.assertEqual(self.server.handle({"id": 1, "method": "x"})["error"]["code"], fs.INVALID_REQUEST)
        self.assertEqual(self.server.handle([])["error"]["code"], fs.INVALID_REQUEST)
        self.assertEqual(rpc(self.server, "no/such")["error"]["code"], fs.METHOD_NOT_FOUND)
        self.assertEqual(rpc(self.server, "tools/call", {"name": "nope"})["error"]["code"], fs.INVALID_PARAMS)
        self.assertEqual(rpc(self.server, "tools/call", {"name": "read_file", "arguments": {}})["error"]["code"],
                         fs.INVALID_PARAMS)
        self.assertEqual(self.call("read_file", path=123)["error"]["code"], fs.INVALID_PARAMS)
        self.assertEqual(self.call("watch_directory", action="bogus")["error"]["code"], fs.INVALID_PARAMS)

    def test_s3_notifications_get_no_response_and_ping(self):
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "notifications/initialized"}))
        self.assertIsNone(self.server.handle({"jsonrpc": "2.0", "method": "no/such"}))  # silent per spec
        self.assertEqual(rpc(self.server, "ping")["result"], {})

    def test_s4_batch_and_id_echo(self):
        out = self.server.handle([
            {"jsonrpc": "2.0", "id": "a", "method": "ping"},
            {"jsonrpc": "2.0", "method": "notifications/initialized"},
            {"jsonrpc": "2.0", "id": 7, "method": "tools/list"}])
        self.assertEqual([o["id"] for o in out], ["a", 7])

    def test_requires_initialize(self):
        fresh = fs.build_server(fs.load_config(None, str(self.root)))
        err = rpc(fresh, "tools/list")["error"]
        self.assertEqual(err["code"], fs.NOT_INITIALIZED)


class TestResources(InProcessBase):
    def test_s5_tools_list_exposes_all_tools(self):
        names = {t["name"] for t in rpc(self.server, "tools/list")["result"]["tools"]}
        self.assertTrue({"list_files", "read_file", "get_file_info", "search_files", "parse_resume",
                         "write_file", "watch_directory", "batch_process",
                         "get_config", "update_config"} <= names)

    def test_s6_resource_discovery(self):
        res = rpc(self.server, "resources/list")["result"]["resources"]
        uris = {r["uri"] for r in res}
        self.assertIn("resume:///resumes/alice_johnson.txt", uris)
        self.assertIn("config://server", uris)
        tpl = rpc(self.server, "resources/templates/list")["result"]["resourceTemplates"]
        self.assertEqual(tpl[0]["uriTemplate"], "resume:///{path}")

    def test_s7_resource_read_and_not_found(self):
        ok = rpc(self.server, "resources/read", {"uri": "resume:///resumes/bob_smith.txt"})
        self.assertIn("Bob Smith", ok["result"]["contents"][0]["text"])
        miss = rpc(self.server, "resources/read", {"uri": "resume:///nope.txt"})
        self.assertEqual(miss["error"]["code"], fs.RESOURCE_NOT_FOUND)
        self.assertEqual(miss["error"]["data"]["uri"], "resume:///nope.txt")
        self.assertEqual(rpc(self.server, "resources/subscribe",
                             {"uri": "resume:///resumes/bob_smith.txt"})["result"], {})


class TestTools(InProcessBase):
    def payload(self, resp):
        self.assertNotIn("error", resp, resp)
        self.assertFalse(resp["result"]["isError"], resp["result"]["content"])
        return resp["result"]["structuredContent"]

    def test_s8_milestone1_tools(self):
        self.assertEqual(self.payload(self.call("list_files", directory="resumes"))["count"], 4)
        txt = self.payload(self.call("read_file", path="resumes/alice_johnson.txt"))["text"]
        self.assertIn("FastAPI", txt)
        info = self.payload(self.call("get_file_info", path="resumes/alice_johnson.txt"))
        self.assertEqual(len(info["sha256"]), 64)
        hits = self.payload(self.call("search_files", query="kubernetes"))
        self.assertGreaterEqual(hits["count"], 1)
        parsed = self.payload(self.call("parse_resume", path="resumes/alice_johnson.txt"))
        self.assertEqual(parsed["emails"], ["alice.johnson@example.com"])
        self.assertIn("EXPERIENCE", parsed["sections"])
        self.assertGreaterEqual(parsed["years_experience"], 8)
        dan = self.payload(self.call("parse_resume", path="resumes/dan_wright.txt"))
        self.assertTrue(dan["phones"])

    def test_s9_sandbox_prevents_traversal(self):
        for bad in ["../../etc/passwd", "/etc/passwd", "resumes/../../x.txt"]:
            r = self.call("read_file", path=bad)["result"]
            self.assertTrue(r["isError"], bad)

    def test_s10_tool_errors_are_isError_not_rpc_errors(self):
        r = self.call("read_file", path="resumes/ghost.txt")
        self.assertTrue(r["result"]["isError"])
        self.assertIn("not found", r["result"]["content"][0]["text"].lower())
        r = self.call("write_file", path="x.exe", content="a")
        self.assertTrue(r["result"]["isError"])

    def test_s11_write_file(self):
        out = self.payload(self.call("write_file", path="reports/t.md", content="# hi"))
        self.assertEqual(out["bytes_written"], 4)
        self.assertTrue((self.root / "reports" / "t.md").exists())
        again = self.call("write_file", path="reports/t.md", content="x", overwrite=False)
        self.assertTrue(again["result"]["isError"])

    def test_s12_batch_process(self):
        out = self.payload(self.call("batch_process", directory="resumes", operation="parse",
                                     include_text=False, max_workers=3))
        self.assertEqual((out["total"], out["succeeded"], out["failed"]), (4, 4, 0))
        self.assertEqual(out["workers"], 3)
        self.assertNotIn("text", out["results"][0]["data"])
        # partial failure must not abort the batch; order preserved
        mixed = self.payload(self.call("batch_process", operation="info", paths=[
            "resumes/alice_johnson.txt", "resumes/missing.txt", "../../etc/passwd"]))
        self.assertEqual([r["status"] for r in mixed["results"]], ["ok", "error", "error"])
        self.assertEqual(mixed["failed"], 2)
        # docx support
        d = self.root / "resumes" / "eve.docx"
        with zipfile.ZipFile(d, "w") as z:
            z.writestr("word/document.xml",
                       '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
                       '<w:body><w:p><w:r><w:t>Eve Adams</w:t></w:r></w:p>'
                       '<w:p><w:r><w:t>Python and Docker, 6 years</w:t></w:r></w:p></w:body></w:document>')
        eve = self.payload(self.call("parse_resume", path="resumes/eve.docx", include_text=True))
        self.assertEqual(eve["name_guess"], "Eve Adams")
        self.assertEqual(eve["years_experience"], 6)
        big = self.call("batch_process", paths=["a.txt"] * 500)
        self.assertTrue(big["result"]["isError"])

    def test_s15_config_management(self):
        cfg = self.payload(self.call("get_config"))
        self.assertEqual(cfg["batch_workers"], 4)
        self.assertEqual(self.payload(self.call("update_config", key="batch_workers", value=8))["value"], 8)
        self.assertEqual(self.payload(self.call("batch_process", directory="resumes"))["workers"], 8)
        self.assertTrue(self.call("update_config", key="root_dir", value="/")["result"]["isError"])
        self.assertTrue(self.call("update_config", key="batch_workers", value="x")["result"]["isError"])
        self.assertTrue(self.call("update_config", key="batch_workers", value=-1)["result"]["isError"])
        self.payload(self.call("update_config", key="allowed_extensions", value=["txt"]))
        self.assertEqual(self.payload(self.call("list_files", directory="resumes"))["count"], 4)
        self.assertEqual(json.loads(rpc(self.server, "resources/read", {"uri": "config://server"})
                                    ["result"]["contents"][0]["text"])["batch_workers"], 8)

    def test_config_file_and_env_precedence(self):
        cfg_file = self.root.parent / "c.json"
        cfg_file.write_text(json.dumps({"root_dir": "/tmp/from_file", "batch_workers": 2}))
        self.assertTrue(fs.load_config(str(cfg_file), None)["root_dir"].endswith("from_file"))
        self.assertEqual(fs.load_config(str(cfg_file), str(self.root))["root_dir"], str(self.root.resolve()))
        self.assertEqual(fs.load_config(str(cfg_file), None)["batch_workers"], 2)


class TestOverStdio(unittest.TestCase):
    """Real subprocess + real JSON-RPC over pipes via MCPClient."""

    def setUp(self):
        self.root = make_root()
        self.client = MCPClient("fs", [sys.executable, SERVER, "--root", str(self.root)], timeout=15)
        self.client.start()

    def tearDown(self):
        self.client.close()
        shutil.rmtree(self.root.parent, ignore_errors=True)

    def test_s13_watch_directory_detects_created_modified_deleted(self):
        c = self.client
        w = c.call_tool("watch_directory", {"action": "start", "directory": "resumes",
                                            "interval_seconds": 0.1})
        self.assertEqual(w["status"], "started")
        self.assertEqual(w["tracked_files"], 4)
        again = c.call_tool("watch_directory", {"action": "start", "directory": "resumes"})
        self.assertEqual(again["status"], "already_running")
        self.assertEqual(c.call_tool("watch_directory", {"action": "list"})["watchers"][0]["watch_id"],
                         w["watch_id"])

        new = self.root / "resumes" / "frank.txt"
        new.write_text("Frank\nPython")
        deadline = time.time() + 5
        seen = []
        while time.time() < deadline and not seen:  # background thread detects it on its own
            time.sleep(0.2)
            seen = [n for n in c.drain_notifications()
                    if n.get("method") == "notifications/resources/list_changed"]
        self.assertTrue(seen, "expected list_changed notification")
        ev = c.call_tool("watch_directory", {"action": "poll", "watch_id": w["watch_id"]})
        self.assertEqual([(e["event"], e["path"]) for e in ev["events"]], [("created", "resumes/frank.txt")])
        self.assertEqual(c.call_tool("watch_directory", {"action": "poll", "watch_id": w["watch_id"]})["count"], 0)

        c.subscribe("resume:///resumes/frank.txt")
        time.sleep(0.05)
        new.write_text("Frank\nPython, Docker, more text")
        ev = c.call_tool("watch_directory", {"action": "poll", "watch_id": w["watch_id"]})
        self.assertEqual(ev["events"][0]["event"], "modified")
        time.sleep(0.3)
        methods = [n["method"] for n in c.drain_notifications()]
        self.assertIn("notifications/resources/updated", methods)

        new.unlink()
        ev = c.call_tool("watch_directory", {"action": "poll", "watch_id": w["watch_id"]})
        self.assertEqual(ev["events"][0]["event"], "deleted")

        self.assertEqual(c.call_tool("watch_directory", {"action": "stop", "watch_id": w["watch_id"]})["status"],
                         "stopped")
        with self.assertRaises(MCPToolError):
            c.call_tool("watch_directory", {"action": "poll", "watch_id": w["watch_id"]})

    def test_s14_protocol_errors_surface_in_client(self):
        with self.assertRaises(MCPError) as cm:
            self.client.request("does/not/exist")
        self.assertEqual(cm.exception.code, fs.METHOD_NOT_FOUND)
        with self.assertRaises(MCPError) as cm:
            self.client.read_resource("resume:///ghost.txt")
        self.assertEqual(cm.exception.code, fs.RESOURCE_NOT_FOUND)
        self.assertEqual(len(self.client.list_tools()), 10)


if __name__ == "__main__":
    unittest.main(verbosity=2)
