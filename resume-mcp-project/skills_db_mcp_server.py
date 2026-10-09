
from __future__ import annotations

import json
import re
import sqlite3
import sys
from typing import List

from filesystem_mcp_server import MCPServer, ToolError, logging

# canonical -> (category, [synonyms])
TAXONOMY = {
    "python": ("language", ["python3", "py"]),
    "java": ("language", []),
    "javascript": ("language", ["js", "ecmascript", "es6"]),
    "typescript": ("language", ["ts"]),
    "go": ("language", ["golang"]),
    "c++": ("language", ["cpp"]),
    "sql": ("database", ["t-sql", "pl/sql"]),
    "postgresql": ("database", ["postgres", "psql"]),
    "mysql": ("database", []),
    "mongodb": ("database", ["mongo"]),
    "redis": ("database", []),
    "react": ("frontend", ["reactjs", "react.js"]),
    "vue": ("frontend", ["vuejs", "vue.js"]),
    "html/css": ("frontend", ["html", "css", "html5", "css3"]),
    "node.js": ("backend", ["nodejs", "node"]),
    "django": ("backend", []),
    "flask": ("backend", []),
    "fastapi": ("backend", []),
    "rest apis": ("backend", ["rest", "restful", "rest api", "restful apis"]),
    "graphql": ("backend", []),
    "docker": ("devops", ["containers", "containerization"]),
    "kubernetes": ("devops", ["k8s"]),
    "aws": ("cloud", ["amazon web services", "ec2", "s3", "lambda"]),
    "gcp": ("cloud", ["google cloud", "google cloud platform"]),
    "azure": ("cloud", ["microsoft azure"]),
    "terraform": ("devops", ["infrastructure as code", "iac"]),
    "ci/cd": ("devops", ["cicd", "continuous integration", "github actions", "jenkins", "gitlab ci"]),
    "git": ("tooling", ["github", "gitlab"]),
    "linux": ("tooling", ["unix", "bash"]),
    "machine learning": ("data", ["ml", "scikit-learn", "sklearn"]),
    "deep learning": ("data", ["pytorch", "tensorflow", "neural networks"]),
    "nlp": ("data", ["natural language processing"]),
    "pandas": ("data", ["numpy"]),
    "data analysis": ("data", ["data analytics", "analytics"]),
    "microservices": ("architecture", ["micro-services", "service-oriented architecture"]),
    "system design": ("architecture", ["distributed systems", "software architecture"]),
    "agile": ("process", ["scrum", "kanban"]),
    "testing": ("process", ["unit testing", "pytest", "tdd", "test automation", "junit"]),
    "leadership": ("soft skill", ["team lead", "mentoring", "mentored", "led a team"]),
    "communication": ("soft skill", ["stakeholder management"]),
}


def build_db() -> sqlite3.Connection:
    db = sqlite3.connect(":memory:", check_same_thread=False)
    db.executescript("""
        CREATE TABLE skills(canonical TEXT PRIMARY KEY, category TEXT NOT NULL);
        CREATE TABLE synonyms(term TEXT PRIMARY KEY, canonical TEXT NOT NULL REFERENCES skills(canonical));
    """)
    for canon, (cat, syns) in TAXONOMY.items():
        db.execute("INSERT INTO skills VALUES (?,?)", (canon, cat))
        for term in {canon, *syns}:
            db.execute("INSERT INTO synonyms VALUES (?,?)", (term.lower(), canon))
    db.commit()
    return db


def build_server() -> MCPServer:
    server = MCPServer("skills-db-mcp", "1.0.0",
                       instructions="Skills taxonomy: normalise text into canonical skills.")
    db = build_db()
    rows = db.execute("SELECT s.term, k.canonical, k.category FROM synonyms s "
                      "JOIN skills k USING(canonical) ORDER BY length(s.term) DESC").fetchall()
    compiled = [(re.compile(r"(?<![\w+#.])" + re.escape(t) + r"(?![\w+#]|\.\w)", re.I), t, c, cat)
                for t, c, cat in rows]

    @server.tool("expand_skills", "Find canonical skills mentioned in free text.",
                 {"text": {"type": "string"}}, ["text"])
    def expand_skills(text: str) -> dict:
        found = {}
        for rx, term, canon, cat in compiled:
            if canon not in found and rx.search(text):
                found[canon] = {"skill": canon, "category": cat, "matched_term": term}
        return {"count": len(found), "skills": sorted(found.values(), key=lambda s: s["skill"])}

    @server.tool("lookup_skill", "Resolve one term to its canonical skill.",
                 {"term": {"type": "string"}}, ["term"])
    def lookup_skill(term: str) -> dict:
        row = db.execute("SELECT k.canonical, k.category FROM synonyms s JOIN skills k "
                         "USING(canonical) WHERE s.term=?", (term.lower().strip(),)).fetchone()
        if not row:
            raise ToolError(f"Unknown skill: {term}")
        syns = [r[0] for r in db.execute("SELECT term FROM synonyms WHERE canonical=?", (row[0],))]
        return {"skill": row[0], "category": row[1], "synonyms": sorted(syns)}

    @server.tool("list_categories", "List skill categories with counts.", {})
    def list_categories() -> dict:
        return {"categories": [{"category": c, "skills": n} for c, n in db.execute(
            "SELECT category, COUNT(*) FROM skills GROUP BY category ORDER BY category")]}

    server.resource_lister = lambda: [{
        "uri": "skills://taxonomy", "name": "Skills taxonomy", "mimeType": "application/json",
        "description": f"{len(TAXONOMY)} canonical skills with synonyms"}]
    server.resource_reader = lambda uri: ([{
        "uri": uri, "mimeType": "application/json",
        "text": json.dumps({k: {"category": v[0], "synonyms": v[1]} for k, v in TAXONOMY.items()}, indent=2)}]
        if uri == "skills://taxonomy" else None)
    return server


if __name__ == "__main__":
    logging.basicConfig(stream=sys.stderr, level="WARNING")
    build_server().serve_stdio()
