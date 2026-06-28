"""Context tree builder — auto-adds nodes as the agent works."""

from __future__ import annotations

import re
import json
from pathlib import Path
from typing import Any, List

try:
    from agent.context_tree.graph import (
        ContextGraph, Node,
        FILE, SYMBOL, TURN, CALL,
        MODIFIED_BY, REFERENCED, TRIGGERED,
    )
except ImportError:
    from .graph import (
        ContextGraph, Node,
        FILE, SYMBOL, TURN, CALL,
        MODIFIED_BY, REFERENCED, TRIGGERED,
    )

# ── Tag patterns (loaded from tags.json, user-extensible) ─────────────────────

_TAGS_FILE = Path(__file__).parent / "tags.json"
_TAG_PATTERNS: list[tuple[re.Pattern, str]] | None = None


def _load_tag_patterns() -> list[tuple[re.Pattern, str]]:
    """Load tag patterns from tags.json. Falls back to hardcoded defaults."""
    global _TAG_PATTERNS
    if _TAG_PATTERNS is not None:
        return _TAG_PATTERNS

    patterns: list[tuple[re.Pattern, str]] = []
    try:
        if _TAGS_FILE.exists():
            data = json.loads(_TAGS_FILE.read_text())
            for tag, regexes in data.items():
                if tag.startswith("_"):
                    continue  # skip _comment, _format etc.
                if isinstance(regexes, list):
                    for rx in regexes:
                        patterns.append((re.compile(rx, re.I), tag))
    except Exception:
        pass

    if not patterns:
        # Hardcoded fallback (same as v1.0)
        defaults = [
            (r"auth|login|token|password|jwt|session|oauth", "auth"),
            (r"secur|crypto|encrypt|hash|cert|tls|ssl", "security"),
            (r"db|database|sql|migration|schema|model|orm", "database"),
            (r"api|endpoint|route|router|rest|graphql", "api"),
            (r"test|spec|fixture|mock|assert", "test"),
            (r"config|settings|env|\.yaml|\.toml|\.ini", "config"),
            (r"component|page|view|tsx|jsx|widget|ui", "ui"),
            (r"deploy|docker|ci|cd|k8s|helm|infra", "infra"),
        ]
        for rx, tag in defaults:
            patterns.append((re.compile(rx, re.I), tag))

    _TAG_PATTERNS = patterns
    return patterns


def reload_tags() -> None:
    """Force reload of tag patterns from tags.json. Call after editing the file."""
    global _TAG_PATTERNS
    _TAG_PATTERNS = None
    _load_tag_patterns()


def _extract_tags(text: str) -> List[str]:
    patterns = _load_tag_patterns()
    return list({tag for pattern, tag in patterns if pattern.search(text)})


def _estimate_complexity(content: str) -> int:
    """Estimate 0-100 complexity from file content."""
    lines = content.count("\n") + 1
    score = min(30, lines // 10)                              # length
    score += min(20, content.count("def ") * 3)              # functions
    score += min(20, content.count("class ") * 5)            # classes
    score += min(15, content.count("import ") * 2)           # imports
    score += min(15, (content.count("if ") + content.count("for ") + content.count("while ")) * 2)
    return min(100, score)


def _file_node_id(path: str) -> str:
    return f"file:{path}"


def _turn_node_id(turn_index: int, session_id: str) -> str:
    return f"turn:{session_id}:{turn_index}"


def _call_node_id(turn_index: int, call_index: int, session_id: str) -> str:
    return f"call:{session_id}:{turn_index}:{call_index}"


# ── Builder ───────────────────────────────────────────────────────────────────

class ContextTreeBuilder:
    """Attach one instance to agent._ctx_builder. Call on_tool_result / on_turn_*."""

    def __init__(self, graph: ContextGraph, session_id: str = ""):
        self.graph      = graph
        self.session_id = session_id
        self._turn_idx  = 0
        self._call_idx  = 0
        self._cur_turn_node_id: str = ""

    # ── Tool results ─────────────────────────────────────────────────────────

    def on_tool_result(
        self,
        tool_name: str,
        tool_input: dict,
        tool_output: str,
        agent: Any = None,
    ) -> None:
        try:
            name = (tool_name or "").lower()
            if name in ("read_file", "view_file", "cat", "search_files"):
                self._handle_read(tool_input, tool_output)
            elif name in ("write_file", "create_file", "edit_file", "str_replace_editor", "patch"):
                self._handle_write(tool_input, tool_output)
            elif name in ("bash", "terminal", "shell"):
                self._handle_bash(tool_input, tool_output)
            elif name in ("delegate_task", "agent"):
                self._handle_delegate(tool_input, tool_output)
        except Exception:
            pass  # never break tool execution

    def _handle_read(self, inp: dict, out: str) -> None:
        path = str(inp.get("path") or inp.get("file_path") or inp.get("filename") or inp.get("pattern") or "")
        if not path:
            return
        node = Node(
            id=_file_node_id(path),
            type=FILE,
            label=path.split("/")[-1],
            tags=_extract_tags(path + " " + out[:500]),
            complexity=_estimate_complexity(out),
            meta={"path": path, "last_op": "read"},
        )
        self.graph.add_node(node)
        if self._cur_turn_node_id:
            self.graph.add_edge(self._cur_turn_node_id, node.id, REFERENCED)

    def _handle_write(self, inp: dict, out: str) -> None:
        path = str(inp.get("path") or inp.get("file_path") or inp.get("filename") or "")
        content = str(inp.get("content") or inp.get("new_str") or inp.get("new_string") or inp.get("new_text") or "")
        if not path:
            return
        node = Node(
            id=_file_node_id(path),
            type=FILE,
            label=path.split("/")[-1],
            tags=_extract_tags(path + " " + content[:500]),
            complexity=_estimate_complexity(content),
            meta={"path": path, "last_op": "write"},
        )
        self.graph.add_node(node)
        if self._cur_turn_node_id:
            self.graph.add_edge(node.id, self._cur_turn_node_id, MODIFIED_BY)

    def _handle_bash(self, inp: dict, out: str) -> None:
        cmd = str(inp.get("command") or inp.get("cmd") or "")[:80]
        self._call_idx += 1
        node = Node(
            id=_call_node_id(self._turn_idx, self._call_idx, self.session_id),
            type=CALL,
            label=f"bash: {cmd[:40]}",
            tags=_extract_tags(cmd),
            complexity=0,
            meta={"command": cmd},
        )
        self.graph.add_node(node)

    def _handle_delegate(self, inp: dict, out: str) -> None:
        """Track sub-agent spawns as CALL nodes."""
        goal = str(inp.get("goal") or inp.get("prompt") or "")[:80]
        self._call_idx += 1
        node = Node(
            id=_call_node_id(self._turn_idx, self._call_idx, self.session_id),
            type=CALL,
            label=f"delegate: {goal[:40]}",
            tags=_extract_tags(goal),
            complexity=0,
            meta={"goal": goal, "type": "delegate"},
        )
        self.graph.add_node(node)
        if self._cur_turn_node_id:
            self.graph.add_edge(self._cur_turn_node_id, node.id, TRIGGERED)

    # ── Turn lifecycle ────────────────────────────────────────────────────────

    def on_turn_start(self, user_message: str, history: List[dict], agent: Any = None) -> None:
        try:
            self._turn_idx += 1
            self._call_idx  = 0
            tags = _extract_tags(user_message)
            node = Node(
                id=_turn_node_id(self._turn_idx, self.session_id),
                type=TURN,
                label=user_message[:60],
                tags=tags,
                complexity=0,
                meta={"message": user_message[:200], "history_depth": len(history)},
            )
            self.graph.add_node(node)
            self._cur_turn_node_id = node.id
        except Exception:
            pass

    def on_turn_end(self, response: str = "", agent: Any = None) -> None:
        try:
            self.graph.save()
        except Exception:
            pass
