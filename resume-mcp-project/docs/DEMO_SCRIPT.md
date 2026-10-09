# Demo video script (5-6 minutes)

Record the terminal with `python demo.py --pause 3` (adds a pause between scenes so you can talk).
Show `docs/workflow_state_machine.png` on screen at 0:00 and 4:00.

| Time | Screen | Say |
|---|---|---|
| 0:00-0:40 | Slides: `workflow_state_machine.png` | Milestone 1 had custom in-process file tools. Milestone 2 moves them into an MCP server so any MCP agent can reuse them; the LangGraph agent becomes a pure MCP client. |
| 0:40-1:30 | `demo.py` Scene 1 | JSON-RPC 2.0 over stdio. `initialize` handshake, capabilities advertise tools and resources (subscribe + listChanged). `tools/list` returns 10 tools with JSON-Schema. Open `filesystem_mcp_server.py` `MCPServer._dispatch`. |
| 1:30-2:10 | Scene 2 | Resource discovery: `resources/list`, templates `resume:///{path}`, `resources/read`. |
| 2:10-2:50 | Scene 3 | Error handling: protocol errors use JSON-RPC codes (-32601, -32602, -32002). Tool failures return `isError:true`. Path traversal blocked by the sandbox. |
| 2:50-3:30 | Scenes 4 and 5 | New capabilities. `batch_process`: thread pool, order preserved, one bad file doesn't fail the batch. `watch_directory`: drop a file, server pushes `list_changed`, client polls events. |
| 3:30-3:50 | Scene 6 + `config/server_config.json` | Config precedence: defaults < file < env `RESUME_MCP_ROOT` < `--root`. Runtime-safe keys only. |
| 3:50-5:00 | Scene 7 | Agent connects to TWO servers (filesystem + skills-db bonus). Show graph path, ranking table, and the call summary: one `batch_process` call instead of N reads. Mention synonym handling (k8s -> kubernetes, Postgres -> postgresql). |
| 5:00-5:40 | Scene 8 | Agent starts `watch_directory`, a resume appears, agent scores only the new file. |
| 5:40-6:00 | Terminal: `python -m pytest tests -v` | 27 tests pass. Wrap up: diagram, tests, bonus server. |

Tip: before recording run `python -m pytest tests -q` to prove everything is green.
