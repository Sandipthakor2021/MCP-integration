# Workflow diagrams

PNG versions: `workflow_state_machine.png` (LangGraph graph) and `mcp_sequence.png` (message flow).
Regenerate with `python docs/make_diagrams.py`. Mermaid sources below render on GitHub / VS Code.

## 1. LangGraph state machine

```mermaid
stateDiagram-v2
    [*] --> connect
    connect --> load_job: initialize + tools/list + resources/list
    load_job --> extract_requirements: tools/call read_file
    extract_requirements --> discover_resumes: tools/call expand_skills (skills server)
    discover_resumes --> batch_parse: resumes found (tools/call list_files)
    discover_resumes --> no_resumes: nothing found
    batch_parse --> score: tools/call batch_process
    score --> report: tools/call expand_skills x N
    report --> [*]: tools/call write_file
    no_resumes --> [*]
```

## 2. Agent <-> MCP interaction

```mermaid
sequenceDiagram
    participant A as LangGraph agent
    participant R as MCPRegistry / MCPClient
    participant F as filesystem MCP server
    participant S as skills-db MCP server
    A->>R: start()
    R->>F: initialize, tools/list, resources/list
    R->>S: initialize, tools/list, resources/list
    A->>R: call("read_file")
    R->>F: tools/call read_file
    A->>R: call("expand_skills")
    R->>S: tools/call expand_skills
    A->>R: call("batch_process", paths)
    R->>F: tools/call batch_process
    F-->>R: results[] (per-file ok/error)
    A->>R: call("write_file", report)
    R->>F: tools/call write_file
    Note over A,F: Watch mode
    A->>R: call("watch_directory", start)
    R->>F: tools/call watch_directory
    F-->>R: notifications/resources/list_changed
    A->>R: call("watch_directory", poll)
    F-->>A: events [created resumes/x.txt]
    A->>A: re-run graph with only_paths
```

## 3. Routing

`MCPRegistry` calls `tools/list` on every server at start-up and builds `tool name -> server`.
Graph nodes only say `reg.call("batch_process", ...)`; they never know which server answers.
Adding a third server (web search, database ...) is one entry in `default_servers()`.
