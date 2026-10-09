# Resume Matching Agent with MCP (Milestone 2)

A LangGraph resume-matching agent whose file-system tools now live in a
**Model Context Protocol (MCP) server** instead of inside the agent.

```
LangGraph agent  --(MCPRegistry / MCPClient)-->  filesystem_mcp_server.py   (10 tools, resources)
 matching_agent.py                         \-->  skills_db_mcp_server.py     (bonus: skills taxonomy)
                   JSON-RPC 2.0 over stdio
```

## Quick start

```bash
pip install -r requirements.txt
python matching_agent.py                 # rank the sample resumes (2 MCP servers)
python matching_agent.py --trace         # ...and print the raw JSON-RPC traffic
python matching_agent.py --watch 30      # rank, then watch resumes/ for 30 s
python demo.py --pause 2                 # narrated walkthrough used for the demo video
python -m pytest tests -v                # 27 tests
python filesystem_mcp_server.py --root ./data   # run the server standalone (stdio)
```

Python 3.10+. The servers and client use only the standard library; `langgraph` is needed for the agent.

## Deliverable map

| Requirement | Where |
|---|---|
| MCP filesystem server | `filesystem_mcp_server.py` |
| JSON-RPC 2.0 compliant (ids, batches, notifications, error codes -32700/-32600/-32601/-32602/-32603) | `MCPServer.handle*` / `_dispatch`; tests `S1-S4` |
| Proper error handling / status codes | protocol errors -> JSON-RPC `error`; tool failures -> `result.isError=true`; resource miss -> `-32002` with `data.uri`; tests `S2, S7, S9, S10, S14` |
| Resource discovery | `resources/list`, `resources/templates/list`, `resources/read`, `resources/subscribe`; `resume:///<path>` and `config://server`; tests `S5-S7` |
| Configuration management | `config/server_config.json`, env `RESUME_MCP_ROOT`, `--root`, tools `get_config` / `update_config` (+ resource `config://server`); test `S15` |
| `watch_directory()` | tool with actions `start / poll / stop / list`; background poller; pushes `notifications/resources/list_changed`, `resources/updated`, `message`; tests `S13`, `A6b` |
| `batch_process()` | tool; thread pool, order-preserving, per-file status, partial failure tolerated, size cap; test `S12` |
| Milestone-1 tools as MCP | `list_files, read_file, get_file_info, search_files, parse_resume, write_file` (see assumption below) |
| Agent refactor | `matching_agent.py`: **no direct file access**; test `A5` asserts all I/O went through MCP |
| Multi-MCP (bonus) | `skills_db_mcp_server.py` (SQLite taxonomy) + `MCPRegistry` routing tools by discovery; tests `A2, A7, A8` |
| State machine / workflow diagram | `docs/workflow_state_machine.png`, `docs/mcp_sequence.png`, `docs/workflow_diagram.md` (Mermaid) |
| Test scenarios | `tests/` (27 tests, results in `docs/TEST_RESULTS.txt`) |
| Demo video | script in `docs/DEMO_SCRIPT.md`; run `python demo.py --pause 3` while recording |

## Assumption you should check

I did not have your Milestone 1 code, so the "Milestone 1 tools" are my reconstruction of a typical
resume file-system toolset: list / read / info / search / parse / write. If your Milestone 1 had
different tool names, rename or add them in `build_server()` (each tool is a one-line
`server.tool(...)(method)` registration backed by a method on `FileSystemService`).

## Design notes

* **Own JSON-RPC layer, no SDK.** `MCPServer` is ~150 lines so every JSON-RPC rule is visible and
  gradable. It speaks MCP 2024-11-05 over newline-delimited stdio. It has been tested with the included
  client; I have not tested it against third-party MCP hosts. If you prefer the official `mcp` SDK
  (`FastMCP`), the tool methods on `FileSystemService` can be reused unchanged.
* **Security.** Every path is resolved inside the sandbox root (traversal and absolute paths rejected);
  extension allow-list; size limit; write allow-list (`.txt/.md/.json`); `root_dir` is not changeable at runtime.
* **Why `batch_process`.** The agent makes one MCP round trip for N resumes (verified by test `A5`).
* **Why a skills server.** Without it the agent only string-matches the job's words; with it,
  `k8s`/`Postgres`/`GitHub Actions` map to canonical skills. Compare `python matching_agent.py`
  with `python matching_agent.py --no-skills-server`.
* **Scoring** (`score_candidate`): 60% required-skill coverage, 20% preferred coverage, 20% experience
  vs. minimum (experience credit is scaled by required coverage so unrelated years don't help).
  Deterministic, no LLM. To add an LLM summary, add a node after `score` in `build_graph()`.
* **watch_directory** is polling-based (portable, no dependencies). `poll` forces an immediate diff,
  so callers never wait for the timer.
* PDF resumes need `pypdf`; DOCX is parsed with the standard library.

## Layout

```
filesystem_mcp_server.py   MCP framework + filesystem server (Part A)
matching_agent.py          LangGraph agent + MCPRegistry (Part B)
mcp_client.py              stdio JSON-RPC client
skills_db_mcp_server.py    bonus second MCP server
demo.py                    narrated demo for the video
config/server_config.json  server configuration
data/{resumes,jobs,reports} sample data
tests/                     27 tests (protocol, resources, tools, watch, agent, multi-MCP)
docs/                      diagrams, demo script, test results, sample output
```
