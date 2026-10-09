
from __future__ import annotations

import argparse
import re
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, TypedDict

from langgraph.graph import END, START, StateGraph

from mcp_client import MCPClient, MCPError, MCPToolError

HERE = Path(__file__).resolve().parent
WEIGHTS = {"required": 0.60, "preferred": 0.20, "experience": 0.20}


# --------------------------------------------------------------------------- #
# Multi-MCP registry
# --------------------------------------------------------------------------- #
class MCPRegistry:
    """Owns several MCP clients and routes tool calls by discovered tool name."""

    def __init__(self, servers: Dict[str, List[str]], trace: bool = False):
        self.clients = {n: MCPClient(n, cmd, trace=trace) for n, cmd in servers.items()}
        self.routes: Dict[str, str] = {}
        self.catalog: Dict[str, Any] = {}
        self.started = False

    def start(self) -> Dict[str, Any]:
        for name, client in self.clients.items():
            client.start()
            tools = client.list_tools()
            resources = client.list_resources()
            self.catalog[name] = {
                "server": client.server_info,
                "tools": [t["name"] for t in tools],
                "resources": [r["uri"] for r in resources],
            }
            for t in tools:
                self.routes.setdefault(t["name"], name)
        self.started = True
        return self.catalog

    def has(self, tool: str) -> bool:
        return tool in self.routes

    def call(self, tool: str, args: Optional[dict] = None) -> Any:
        if tool not in self.routes:
            raise MCPError(-32601, f"No connected MCP server exposes tool '{tool}'")
        return self.clients[self.routes[tool]].call_tool(tool, args)

    def close(self) -> None:
        for c in self.clients.values():
            c.close()
        self.started = False

    @property
    def call_log(self) -> List[dict]:
        return [e for c in self.clients.values() for e in c.call_log]


def default_servers(root: str, with_skills: bool = True) -> Dict[str, List[str]]:
    servers = {"filesystem": [sys.executable, str(HERE / "filesystem_mcp_server.py"), "--root", root]}
    if with_skills:
        servers["skills"] = [sys.executable, str(HERE / "skills_db_mcp_server.py")]
    return servers


# --------------------------------------------------------------------------- #
# Job-description parsing + scoring (pure functions, easy to test)
# --------------------------------------------------------------------------- #
HEADERS = ("title", "company", "about", "required skills", "preferred skills",
           "minimum experience", "responsibilities")


def parse_job(text: str) -> Dict[str, str]:
    sections: Dict[str, List[str]] = {}
    current = None
    for line in text.splitlines():
        m = re.match(r"^\s*([A-Za-z ]+):\s*(.*)$", line)
        if m and m.group(1).strip().lower() in HEADERS:
            current = m.group(1).strip().lower()
            sections[current] = [m.group(2)] if m.group(2) else []
        elif current:
            sections[current].append(line)
    return {k: "\n".join(v).strip() for k, v in sections.items()}


def split_terms(text: str) -> List[str]:
    return [t.strip().lower() for t in re.split(r"[,\n;]", text) if t.strip()]


def score_candidate(req: List[str], pref: List[str], cand_skills: List[str],
                    years: float, min_years: float) -> Dict[str, Any]:
    cs = set(cand_skills)
    req_hit = [s for s in req if s in cs]
    pref_hit = [s for s in pref if s in cs]
    req_cov = len(req_hit) / len(req) if req else 1.0
    pref_cov = len(pref_hit) / len(pref) if pref else 1.0
    # Experience only counts in proportion to relevant skills (10 yrs of marketing != backend experience)
    exp = (min(1.0, years / min_years) if min_years else 1.0) * req_cov
    score = 100 * (WEIGHTS["required"] * req_cov + WEIGHTS["preferred"] * pref_cov
                   + WEIGHTS["experience"] * exp)
    grade = "Strong" if score >= 75 else "Moderate" if score >= 50 else "Weak"
    return {"score": round(score, 1), "grade": grade,
            "required_matched": req_hit, "required_missing": [s for s in req if s not in cs],
            "preferred_matched": pref_hit, "preferred_missing": [s for s in pref if s not in cs],
            "experience_ratio": round(exp, 2)}


# --------------------------------------------------------------------------- #
# LangGraph state + nodes
# --------------------------------------------------------------------------- #
class AgentState(TypedDict, total=False):
    job_path: str
    resumes_dir: str
    report_path: str
    only_paths: Optional[List[str]]
    job_text: str
    job: Dict[str, str]
    required: List[str]
    preferred: List[str]
    min_years: float
    resume_paths: List[str]
    parsed: List[dict]
    skipped: List[dict]
    ranking: List[dict]
    report: str
    discovery: Dict[str, Any]
    steps: List[str]
    error: str


def _skills_in(reg: MCPRegistry, text: str, vocab: List[str]) -> List[str]:
    """Canonical skills in `text` - via the skills MCP server if connected, else naive match."""
    if reg.has("expand_skills"):
        return [s["skill"] for s in reg.call("expand_skills", {"text": text})["skills"]]
    low = text.lower()
    return [v for v in vocab if re.search(r"(?<!\w)" + re.escape(v) + r"(?!\w)", low)]


def build_graph(reg: MCPRegistry):
    def mark(state: AgentState, step: str) -> List[str]:
        return [*state.get("steps", []), step]

    def connect(state: AgentState) -> AgentState:
        catalog = reg.start() if not reg.started else reg.catalog
        return {"discovery": catalog, "steps": mark(state, "connect")}

    def load_job(state: AgentState) -> AgentState:
        text = reg.call("read_file", {"path": state["job_path"]})["text"]
        return {"job_text": text, "job": parse_job(text), "steps": mark(state, "load_job")}

    def extract_requirements(state: AgentState) -> AgentState:
        job = state["job"]
        req_text, pref_text = job.get("required skills", ""), job.get("preferred skills", "")
        if reg.has("expand_skills"):
            req = _skills_in(reg, req_text, [])
            pref = [s for s in _skills_in(reg, pref_text, []) if s not in req]
        else:
            req, pref = split_terms(req_text), split_terms(pref_text)
        m = re.search(r"(\d+)", job.get("minimum experience", ""))
        return {"required": req, "preferred": pref, "min_years": float(m.group(1)) if m else 0.0,
                "steps": mark(state, "extract_requirements")}

    def discover_resumes(state: AgentState) -> AgentState:
        if state.get("only_paths"):
            paths = list(state["only_paths"])
        else:
            files = reg.call("list_files", {"directory": state["resumes_dir"]})["files"]
            paths = [f["path"] for f in files]
        return {"resume_paths": paths, "steps": mark(state, "discover_resumes")}

    def route_after_discovery(state: AgentState) -> str:
        return "batch_parse" if state.get("resume_paths") else "no_resumes"

    def no_resumes(state: AgentState) -> AgentState:
        return {"ranking": [], "report": "No resumes found.", "steps": mark(state, "no_resumes")}

    def batch_parse(state: AgentState) -> AgentState:
        res = reg.call("batch_process", {"paths": state["resume_paths"], "operation": "parse",
                                         "include_text": True})
        ok = [r["data"] for r in res["results"] if r["status"] == "ok"]
        bad = [{"path": r["path"], "error": r["error"]} for r in res["results"] if r["status"] == "error"]
        return {"parsed": ok, "skipped": bad, "steps": mark(state, "batch_parse")}

    def score(state: AgentState) -> AgentState:
        vocab = state["required"] + state["preferred"]
        ranking = []
        for r in state["parsed"]:
            skills = _skills_in(reg, r["text"], vocab)
            s = score_candidate(state["required"], state["preferred"], skills,
                                r["years_experience"], state["min_years"])
            ranking.append({"path": r["path"], "name": r["name_guess"],
                            "email": (r["emails"] or [""])[0],
                            "years": r["years_experience"], "skills_found": sorted(skills), **s})
        ranking.sort(key=lambda x: (-x["score"], x["name"]))
        for i, row in enumerate(ranking, 1):
            row["rank"] = i
        return {"ranking": ranking, "steps": mark(state, "score")}

    def report(state: AgentState) -> AgentState:
        job = state["job"]
        lines = [f"# Candidate ranking - {job.get('title', 'Job')} ({job.get('company', '')})", "",
                 f"Required: {', '.join(state['required'])}  ",
                 f"Preferred: {', '.join(state['preferred'])}  ",
                 f"Minimum experience: {state['min_years']:.0f} years", "",
                 "| Rank | Candidate | Score | Grade | Years | Missing required |",
                 "|---|---|---|---|---|---|"]
        for r in state["ranking"]:
            lines.append(f"| {r['rank']} | {r['name']} | {r['score']} | {r['grade']} | "
                         f"{r['years']} | {', '.join(r['required_missing']) or '-'} |")
        for s in state.get("skipped", []):
            lines.append(f"\n> Skipped {s['path']}: {s['error']}")
        text = "\n".join(lines) + "\n"
        reg.call("write_file", {"path": state["report_path"], "content": text})
        return {"report": text, "steps": mark(state, "report")}

    g = StateGraph(AgentState)
    for name, fn in [("connect", connect), ("load_job", load_job),
                     ("extract_requirements", extract_requirements),
                     ("discover_resumes", discover_resumes), ("no_resumes", no_resumes),
                     ("batch_parse", batch_parse), ("score", score), ("report", report)]:
        g.add_node(name, fn)
    g.add_edge(START, "connect")
    g.add_edge("connect", "load_job")
    g.add_edge("load_job", "extract_requirements")
    g.add_edge("extract_requirements", "discover_resumes")
    g.add_conditional_edges("discover_resumes", route_after_discovery,
                            {"batch_parse": "batch_parse", "no_resumes": "no_resumes"})
    g.add_edge("batch_parse", "score")
    g.add_edge("score", "report")
    g.add_edge("report", END)
    g.add_edge("no_resumes", END)
    return g.compile()


# --------------------------------------------------------------------------- #
# Public API + CLI
# --------------------------------------------------------------------------- #
def run_matching(reg: MCPRegistry, job_path: str = "jobs/job_description.txt",
                 resumes_dir: str = "resumes", report_path: str = "reports/ranking.md",
                 only_paths: Optional[List[str]] = None) -> AgentState:
    graph = build_graph(reg)
    return graph.invoke({"job_path": job_path, "resumes_dir": resumes_dir,
                         "report_path": report_path, "only_paths": only_paths})


def watch_and_match(reg: MCPRegistry, seconds: float, resumes_dir: str = "resumes",
                    interval: float = 1.0, **kw) -> List[AgentState]:
    """Use the watch_directory MCP tool; score every resume that appears while watching."""
    if not reg.started:
        reg.start()
    wid = reg.call("watch_directory", {"action": "start", "directory": resumes_dir,
                                       "interval_seconds": interval})["watch_id"]
    results, end = [], time.time() + seconds
    print(f"[watch] {wid} monitoring '{resumes_dir}' for {seconds:.0f}s ...")
    try:
        while time.time() < end:
            time.sleep(interval)
            events = reg.call("watch_directory", {"action": "poll", "watch_id": wid})["events"]
            new = [e["path"] for e in events if e["event"] in ("created", "modified")]
            if new:
                print(f"[watch] new resumes: {new}")
                results.append(run_matching(reg, resumes_dir=resumes_dir,
                                            report_path="reports/ranking_incremental.md",
                                            only_paths=new, **kw))
    finally:
        reg.call("watch_directory", {"action": "stop", "watch_id": wid})
    return results


def main() -> None:
    ap = argparse.ArgumentParser(description="Resume matching agent (LangGraph + MCP)")
    ap.add_argument("--root", default=str(HERE / "data"), help="MCP sandbox root")
    ap.add_argument("--job", default="jobs/job_description.txt")
    ap.add_argument("--resumes", default="resumes")
    ap.add_argument("--no-skills-server", action="store_true", help="single-MCP mode")
    ap.add_argument("--watch", type=float, default=0, help="after ranking, watch N seconds for new resumes")
    ap.add_argument("--trace", action="store_true", help="print raw JSON-RPC traffic")
    a = ap.parse_args()

    reg = MCPRegistry(default_servers(a.root, not a.no_skills_server), trace=a.trace)
    try:
        state = run_matching(reg, a.job, a.resumes)
        print("Connected MCP servers:")
        for name, info in state["discovery"].items():
            print(f"  - {name}: {len(info['tools'])} tools, {len(info['resources'])} resources")
        print("Graph path:", " -> ".join(state["steps"]), "\n")
        print(state["report"])
        if a.watch:
            watch_and_match(reg, a.watch, a.resumes, job_path=a.job)
    finally:
        reg.close()


if __name__ == "__main__":
    main()
