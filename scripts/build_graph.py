#!/usr/bin/env python3
"""build_graph.py — PostToolUse / UserPromptSubmit hook for Claude Code.

Automatically builds the context graph for every conversation — no manual
invocation required. Register in ~/.claude/settings.json:

  PostToolUse (matcher: "Read|Write|Edit|MultiEdit|Bash|Glob|Grep|Agent|Task"):
    python3 /path/to/scripts/build_graph.py

  UserPromptSubmit:
    python3 /path/to/scripts/build_graph.py

Reads a JSON event from stdin (Claude Code hook format):
  {
    "session_id": "...",
    "tool_name": "Read",          # for PostToolUse
    "tool_input": {...},
    "tool_response": "...",
    "type": "postToolUse" | "userPromptSubmit",
    "message": "..."              # for UserPromptSubmit
  }

Writes nodes to ~/.hermes/router-logs/context-graph-{session_id}.json
and symlinks the latest to context-graph.json for the proxy to read.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

# ── Paths ─────────────────────────────────────────────────────────────────────

_REPO = Path(__file__).parent.parent
_GRAPH_DIR = Path.home() / ".hermes" / "router-logs"
_GRAPH_LINK = _GRAPH_DIR / "context-graph.json"

sys.path.insert(0, str(_REPO))


# ── Graph helpers ─────────────────────────────────────────────────────────────

def _graph_path(session_id: str) -> Path:
    if session_id:
        return _GRAPH_DIR / f"context-graph-{session_id}.json"
    return _GRAPH_LINK


def _load_graph(session_id: str):
    try:
        from hermes.agent.context_tree.graph import ContextGraph
        return ContextGraph.load(_graph_path(session_id), session_id=session_id)
    except Exception:
        return None


def _save_graph(graph, session_id: str) -> None:
    try:
        path = _graph_path(session_id)
        graph.save(path)
        # symlink context-graph.json → latest session graph (for proxy)
        if session_id:
            try:
                if _GRAPH_LINK.is_symlink() or _GRAPH_LINK.exists():
                    _GRAPH_LINK.unlink()
                _GRAPH_LINK.symlink_to(path.name)
            except Exception:
                # fallback: just copy
                try:
                    import shutil
                    shutil.copy2(str(path), str(_GRAPH_LINK))
                except Exception:
                    pass
    except Exception:
        pass


# ── Event handlers ────────────────────────────────────────────────────────────

def handle_post_tool_use(event: dict) -> None:
    session_id = event.get("session_id", "")
    tool_name  = (event.get("tool_name") or event.get("tool_use_name") or "").lower()
    tool_input = event.get("tool_input") or event.get("input") or {}
    tool_output = str(event.get("tool_response") or event.get("output") or "")

    # Normalize tool names (Claude Code uses these exact names)
    _READ_TOOLS  = {"read", "read_file", "view_file"}
    _WRITE_TOOLS = {"write", "write_file", "create_file", "edit", "edit_file",
                    "multiedit", "str_replace_editor", "patch", "multiEdit"}
    _BASH_TOOLS  = {"bash", "terminal", "shell"}
    _SEARCH_TOOLS = {"glob", "grep", "search_files"}
    _AGENT_TOOLS  = {"agent", "task", "delegate_task"}

    graph = _load_graph(session_id)
    if graph is None:
        return

    try:
        from hermes.agent.context_tree.builder import ContextTreeBuilder
        builder = ContextTreeBuilder(graph, session_id=session_id)

        # Synthetic turn node id — reuse graph's recent list to find current turn
        # We don't have a persistent builder across calls, so we approximate
        # turn_idx from node count
        turn_nodes = [n for n in graph.all_nodes() if n.type == "TURN"]
        builder._turn_idx = len(turn_nodes)
        call_nodes = [n for n in graph.all_nodes() if n.type == "CALL"]
        builder._call_idx = len(call_nodes)

        if graph.recent_ids(1):
            latest = graph.recent_ids(1)[0]
            if latest.startswith("turn:"):
                builder._cur_turn_node_id = latest

        if tool_name in _READ_TOOLS or tool_name in _SEARCH_TOOLS:
            builder.on_tool_result(
                tool_name="read_file",
                tool_input=tool_input if isinstance(tool_input, dict) else {},
                tool_output=tool_output,
            )
        elif tool_name in _WRITE_TOOLS:
            builder.on_tool_result(
                tool_name="write_file",
                tool_input=tool_input if isinstance(tool_input, dict) else {},
                tool_output=tool_output,
            )
        elif tool_name in _BASH_TOOLS:
            builder.on_tool_result(
                tool_name="bash",
                tool_input=tool_input if isinstance(tool_input, dict) else {},
                tool_output=tool_output,
            )
        elif tool_name in _AGENT_TOOLS:
            builder.on_tool_result(
                tool_name="delegate_task",
                tool_input=tool_input if isinstance(tool_input, dict) else {},
                tool_output=tool_output,
            )

    except Exception as e:
        # Never crash — write debug only
        try:
            (_GRAPH_DIR / "hook-errors.log").open("a").write(
                f"build_graph post_tool_use error: {e}\n"
            )
        except Exception:
            pass
        return

    _save_graph(graph, session_id)


def handle_user_prompt_submit(event: dict) -> None:
    session_id = event.get("session_id", "")
    message    = str(event.get("message") or event.get("user_message") or "")
    history    = event.get("conversation_history") or []

    graph = _load_graph(session_id)
    if graph is None:
        # First call for this session — create fresh graph
        try:
            from hermes.agent.context_tree.graph import ContextGraph
            graph = ContextGraph(session_id=session_id)
        except Exception:
            return

    try:
        from hermes.agent.context_tree.builder import ContextTreeBuilder
        builder = ContextTreeBuilder(graph, session_id=session_id)
        turn_nodes = [n for n in graph.all_nodes() if n.type == "TURN"]
        builder._turn_idx = len(turn_nodes)
        builder.on_turn_start(
            user_message=message,
            history=list(history) if isinstance(history, list) else [],
        )
    except Exception as e:
        try:
            (_GRAPH_DIR / "hook-errors.log").open("a").write(
                f"build_graph user_prompt error: {e}\n"
            )
        except Exception:
            pass
        return

    _save_graph(graph, session_id)


# ── Main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    _GRAPH_DIR.mkdir(parents=True, exist_ok=True)

    # Read event from stdin
    try:
        raw = sys.stdin.read()
        event = json.loads(raw) if raw.strip() else {}
    except Exception:
        event = {}

    # Also accept env var hints (some hook runners pass data this way)
    if not event.get("session_id"):
        event["session_id"] = os.getenv("CLAUDE_SESSION_ID", "")
    if not event.get("tool_name"):
        event["tool_name"] = os.getenv("CLAUDE_TOOL_NAME", "")

    etype = (event.get("type") or "").lower()

    if etype in ("userpromptsubmit", "user_prompt_submit", ""):
        # Could be either — check fields
        if event.get("tool_name") or event.get("tool_use_name"):
            handle_post_tool_use(event)
        elif event.get("message") or event.get("user_message"):
            handle_user_prompt_submit(event)
        else:
            # Unknown event — try post_tool_use (safe fallback)
            if event.get("tool_response") or event.get("output"):
                handle_post_tool_use(event)
    elif etype in ("posttooluse", "post_tool_use"):
        handle_post_tool_use(event)
    elif etype in ("userpromptsubmit", "user_prompt_submit"):
        handle_user_prompt_submit(event)


if __name__ == "__main__":
    main()
