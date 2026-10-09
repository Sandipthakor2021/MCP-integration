"""Regenerate docs/*.png:  python docs/make_diagrams.py"""
import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import FancyBboxPatch
from pathlib import Path

OUT = Path(__file__).parent
BLUE, GREEN, AMBER, GREY = "#dbeafe", "#dcfce7", "#fef3c7", "#f1f5f9"


def box(ax, x, y, w, h, text, color, fs=9, bold=False):
    ax.add_patch(FancyBboxPatch((x - w / 2, y - h / 2), w, h, boxstyle="round,pad=0.02,rounding_size=0.12",
                                fc=color, ec="#334155", lw=1.2))
    ax.text(x, y, text, ha="center", va="center", fontsize=fs, fontweight="bold" if bold else "normal")


def arrow(ax, p, q, label=None, style="-|>", color="#334155", ls="-", off=(0, 0.14), rad=0.0):
    ax.annotate("", xy=q, xytext=p, arrowprops=dict(arrowstyle=style, color=color, lw=1.3, ls=ls,
                                                    connectionstyle=f"arc3,rad={rad}"))
    if label:
        ax.text((p[0] + q[0]) / 2 + off[0], (p[1] + q[1]) / 2 + off[1], label, ha="center",
                fontsize=7.5, color=color, style="italic")


# ---------------------------------------------------------------- 1. state machine
fig, ax = plt.subplots(figsize=(15, 6.4))
ax.set_xlim(0, 15); ax.set_ylim(0, 6.4); ax.axis("off")
ax.set_title("LangGraph state machine  -  agent  <->  MCP servers", fontsize=14, fontweight="bold")

nodes = [("connect", "MCP: initialize\n+ tools/list\n+ resources/list"),
         ("load_job", "MCP: read_file\n(job description)"),
         ("extract_\nrequirements", "MCP: expand_skills\n(skills server)"),
         ("discover_\nresumes", "MCP: list_files\n(or only_paths)")]
xs = [1.95, 4.5, 7.05, 9.6]
y = 4.6
box(ax, 0.5, y, 0.7, 0.5, "START", GREY, 8, True)
arrow(ax, (0.85, y), (1.0, y))
for (n, calls), x in zip(nodes, xs):
    box(ax, x, y, 1.9, 0.95, n, BLUE, 10, True)
    box(ax, x, y - 1.35, 2.0, 0.85, calls, GREEN, 8)
    arrow(ax, (x, y - 0.48), (x, y - 0.92), style="<|-|>", color="#16a34a", ls="--")
for a, b in zip(xs, xs[1:]):
    arrow(ax, (a + 0.95, y), (b - 0.95, y))

box(ax, 12.0, y, 1.9, 0.95, "has resumes?", AMBER, 10, True)
arrow(ax, (9.6 + 0.95, y), (12.0 - 0.95, y))
box(ax, 13.9, 5.55, 1.6, 0.7, "no_resumes", BLUE, 9, True)
arrow(ax, (12.4, y + 0.45), (13.5, 5.35), "no", off=(-0.2, 0.15))
box(ax, 14.55, 4.7, 0.8, 0.5, "END", GREY, 8, True)
arrow(ax, (14.1, 5.2), (14.4, 4.95))

y2 = 1.55
box(ax, 12.0, y2, 1.9, 0.95, "batch_parse", BLUE, 10, True)
arrow(ax, (12.0, y - 0.48), (12.0, y2 + 0.48), "yes", off=(0.3, 0))
box(ax, 12.0, 0.3, 2.3, 0.5, "MCP: batch_process(parse)", GREEN, 8)
arrow(ax, (12.0, y2 - 0.48), (12.0, 0.55), style="<|-|>", color="#16a34a", ls="--")
box(ax, 8.9, y2, 1.9, 0.95, "score", BLUE, 10, True)
arrow(ax, (12.0 - 0.95, y2), (8.9 + 0.95, y2))
box(ax, 8.9, 0.3, 2.3, 0.5, "MCP: expand_skills per resume", GREEN, 8)
arrow(ax, (8.9, y2 - 0.48), (8.9, 0.55), style="<|-|>", color="#16a34a", ls="--")
box(ax, 5.8, y2, 1.9, 0.95, "report", BLUE, 10, True)
arrow(ax, (8.9 - 0.95, y2), (5.8 + 0.95, y2))
box(ax, 5.8, 0.3, 2.3, 0.5, "MCP: write_file(ranking.md)", GREEN, 8)
arrow(ax, (5.8, y2 - 0.48), (5.8, 0.55), style="<|-|>", color="#16a34a", ls="--")
box(ax, 3.2, y2+0.0, 0.8, 0.5, "END", GREY, 8, True)
arrow(ax, (5.8 - 0.95, y2), (3.6, y2))

box(ax, 1.9, 0.5, 3.2, 0.9, "watch mode (optional loop)\nwatch_directory start / poll\n-> re-enter graph with only_paths", AMBER, 8)
ax.text(7.0, 2.45, "blue = LangGraph node      green = MCP request/response (JSON-RPC 2.0)",
        fontsize=8.5, ha="center", color="#475569")
fig.savefig(OUT / "workflow_state_machine.png", dpi=150, bbox_inches="tight")
plt.close(fig)

# ---------------------------------------------------------------- 2. sequence
fig, ax = plt.subplots(figsize=(13, 8.2))
ax.set_xlim(0, 13); ax.set_ylim(0, 8.2); ax.axis("off")
ax.set_title("Sequence: LangGraph agent  <->  MCP client  <->  MCP servers (JSON-RPC 2.0 over stdio)",
             fontsize=13, fontweight="bold")
lanes = {"LangGraph agent": 1.3, "MCPRegistry /\nMCPClient": 4.3, "filesystem\nMCP server": 8.0, "skills-db\nMCP server": 11.4}
for name, x in lanes.items():
    box(ax, x, 7.6, 2.3, 0.7, name, BLUE, 9, True)
    ax.plot([x, x], [0.2, 7.2], color="#94a3b8", lw=1, ls="--")
A, C, F, S = lanes.values()
msgs = [
    (C, F, 6.8, "initialize / notifications/initialized", False),
    (C, F, 6.4, "tools/list, resources/list  (discovery)", False),
    (C, S, 6.0, "initialize + tools/list  (2nd server)", False),
    (A, C, 5.6, "call('read_file', job)", False),
    (C, F, 5.6, "tools/call read_file", False),
    (A, C, 5.15, "call('expand_skills', required text)", False),
    (C, S, 5.15, "tools/call expand_skills", False),
    (A, C, 4.7, "call('list_files', resumes/)", False),
    (C, F, 4.7, "tools/call list_files", False),
    (A, C, 4.25, "call('batch_process', paths)", False),
    (C, F, 4.25, "tools/call batch_process  (thread pool)", False),
    (F, C, 3.85, "result: parsed resumes + per-file status", True),
    (A, C, 3.4, "call('expand_skills', resume text) x N", False),
    (C, S, 3.4, "tools/call expand_skills", False),
    (A, C, 2.95, "call('write_file', reports/ranking.md)", False),
    (C, F, 2.95, "tools/call write_file", False),
]
for p, q, yy, label, ret in msgs:
    arrow(ax, (p, yy), (q, yy), label, ls="--" if ret else "-", off=(0, 0.1))
ax.add_patch(FancyBboxPatch((6.4, 0.35), 6.2, 1.9, boxstyle="round,pad=0.02,rounding_size=0.1",
                            fc=AMBER, ec="#b45309", lw=1.1, zorder=3))
ax.text(9.5, 1.3, "watch_directory (server-side, new capability)\n\n"
        "start -> background poller thread per watcher\n"
        "new file -> notifications/resources/list_changed\n"
        "agent poll -> events [created|modified|deleted]\n"
        "agent re-runs graph for only the new resume paths", ha="center", va="center", fontsize=8.3, zorder=4)
ax.text(1.3, 1.3, "Errors:\nprotocol -> JSON-RPC error {code,message}\ntool -> result.isError = true\n(agent logs & skips file)",
        fontsize=8, va="center", ha="center", color="#7f1d1d", bbox=dict(fc="white", ec="#7f1d1d", boxstyle="round"), zorder=4)
fig.savefig(OUT / "mcp_sequence.png", dpi=150, bbox_inches="tight")
plt.close(fig)
print("ok")
