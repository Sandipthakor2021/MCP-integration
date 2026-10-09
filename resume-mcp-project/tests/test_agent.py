
import shutil
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import matching_agent as agent
from mcp_client import MCPClient, MCPError

PROJECT = Path(__file__).resolve().parents[1]


class AgentBase(unittest.TestCase):
    with_skills = True

    def setUp(self):
        tmp = Path(tempfile.mkdtemp(prefix="agent_test_"))
        self.root = tmp / "data"
        shutil.copytree(PROJECT / "data", self.root)
        self.reg = agent.MCPRegistry(agent.default_servers(str(self.root), self.with_skills))

    def tearDown(self):
        self.reg.close()
        shutil.rmtree(self.root.parent, ignore_errors=True)


class TestPure(unittest.TestCase):
    def test_a1_scoring(self):
        full = agent.score_candidate(["a", "b"], ["c"], ["a", "b", "c"], 10, 5)
        self.assertEqual(full["score"], 100.0)
        none = agent.score_candidate(["a", "b"], ["c"], [], 0, 5)
        self.assertEqual((none["score"], none["grade"]), (0.0, "Weak"))
        half = agent.score_candidate(["a", "b"], [], ["a"], 5, 5)
        self.assertEqual(half["required_missing"], ["b"])
        self.assertEqual(half["score"], 60.0)  # 100*(0.6*.5 + .2*1 + .2*(1*.5))
        job = agent.parse_job((PROJECT / "data/jobs/job_description.txt").read_text())
        self.assertEqual(job["title"], "Senior Backend Engineer")
        self.assertIn("Docker", job["required skills"])


class TestEndToEnd(AgentBase):
    def test_a2_full_workflow_multi_mcp(self):
        st = agent.run_matching(self.reg)
        self.assertEqual(st["steps"], ["connect", "load_job", "extract_requirements", "discover_resumes",
                                       "batch_parse", "score", "report"])
        names = [r["name"] for r in st["ranking"]]
        self.assertEqual(names, ["Alice Johnson", "Bob Smith", "Carol Diaz", "Dan Wright"])
        self.assertEqual(st["ranking"][0]["grade"], "Strong")
        self.assertEqual(st["ranking"][-1]["grade"], "Weak")
        self.assertEqual(set(st["required"]), {"python", "rest apis", "postgresql", "docker", "aws", "git"})
        # synonyms resolved through the skills MCP server (Postgres->postgresql, k8s->kubernetes)
        self.assertIn("postgresql", st["ranking"][0]["skills_found"])
        self.assertIn("kubernetes", st["ranking"][0]["skills_found"])
        report = (self.root / "reports" / "ranking.md").read_text()
        self.assertIn("Alice Johnson", report)
        self.assertEqual(set(st["discovery"]), {"filesystem", "skills"})

    def test_a5_agent_does_all_io_through_mcp(self):
        agent.run_matching(self.reg)
        tools = [(e["server"], e["tool"]) for e in self.reg.call_log]
        self.assertIn(("filesystem", "read_file"), tools)
        self.assertIn(("filesystem", "list_files"), tools)
        self.assertIn(("filesystem", "batch_process"), tools)
        self.assertIn(("filesystem", "write_file"), tools)
        self.assertIn(("skills", "expand_skills"), tools)
        self.assertEqual(sum(t == ("filesystem", "batch_process") for t in tools), 1)  # one batch, not N calls
        src = (PROJECT / "matching_agent.py").read_text()
        for forbidden in ("open(", "os.listdir", "glob.glob", "read_text("):
            self.assertNotIn(forbidden, src.split("# Public API + CLI")[0].split("def build_graph")[1])

    def test_a6_incremental_new_resume(self):
        agent.run_matching(self.reg)
        (self.root / "resumes" / "grace.txt").write_text(
            "Grace Lee\ngrace@example.com\nSUMMARY\n7 years of experience\nSKILLS\n"
            "Python, FastAPI, REST, Postgres, Docker, AWS, Git, Kubernetes, Terraform\n")
        st = agent.run_matching(self.reg, only_paths=["resumes/grace.txt"],
                                report_path="reports/ranking_incremental.md")
        self.assertEqual(len(st["ranking"]), 1)
        self.assertEqual(st["ranking"][0]["name"], "Grace Lee")
        self.assertGreater(st["ranking"][0]["score"], 85)

    def test_a6b_watch_mode_picks_up_new_file(self):
        import threading, time
        def drop():
            time.sleep(1.2)
            (self.root / "resumes" / "heidi.txt").write_text(
                "Heidi\nSKILLS\nPython, Docker, AWS, Git, REST APIs, PostgreSQL\n5 years of experience")
        t = threading.Thread(target=drop); t.start()
        out = agent.watch_and_match(self.reg, seconds=4, interval=0.3)
        t.join()
        self.assertEqual(len(out), 1)
        self.assertEqual(out[0]["ranking"][0]["name"], "Heidi")

    def test_a7_routing_and_unknown_tool(self):
        self.reg.start()
        self.assertEqual(self.reg.routes["batch_process"], "filesystem")
        self.assertEqual(self.reg.routes["expand_skills"], "skills")
        with self.assertRaises(MCPError):
            self.reg.call("send_email", {})

    def test_a4_empty_directory_branch(self):
        (self.root / "empty").mkdir()
        st = agent.run_matching(self.reg, resumes_dir="empty")
        self.assertEqual(st["steps"][-1], "no_resumes")
        self.assertEqual(st["ranking"], [])

    def test_corrupt_file_is_skipped_not_fatal(self):
        (self.root / "resumes" / "broken.docx").write_bytes(b"not a zip")
        st = agent.run_matching(self.reg)
        self.assertEqual(len(st["ranking"]), 4)
        self.assertEqual(st["skipped"][0]["path"], "resumes/broken.docx")


class TestSingleServer(AgentBase):
    with_skills = False

    def test_a3_works_without_skills_server(self):
        st = agent.run_matching(self.reg)
        self.assertEqual(set(st["discovery"]), {"filesystem"})
        self.assertEqual(st["ranking"][0]["name"], "Alice Johnson")
        self.assertEqual(st["ranking"][-1]["name"], "Dan Wright")


class TestSkillsServer(unittest.TestCase):
    def test_a8_expand_and_lookup(self):
        with MCPClient("skills", [sys.executable, str(PROJECT / "skills_db_mcp_server.py")]) as c:
            got = {s["skill"] for s in c.call_tool("expand_skills", {
                "text": "Built k8s clusters, ReactJS UIs, C++ and Node.js; used Postgres"})["skills"]}
            self.assertEqual(got, {"kubernetes", "react", "c++", "node.js", "postgresql"})
            self.assertEqual(c.call_tool("lookup_skill", {"term": "K8S"})["skill"], "kubernetes")
            self.assertEqual(c.read_resource("skills://taxonomy").count("category") >= 40, True)
            self.assertGreater(len(c.call_tool("list_categories")["categories"]), 5)


if __name__ == "__main__":
    unittest.main(verbosity=2)
