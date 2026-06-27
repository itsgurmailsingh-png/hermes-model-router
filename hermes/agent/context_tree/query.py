"""Context tree query — returns relevant nodes for a prompt."""

from __future__ import annotations

import re
from typing import List

try:
    from agent.context_tree.graph import ContextGraph, Node, FILE, TURN, SYMBOL
except ImportError:
    from .graph import ContextGraph, Node, FILE, TURN, SYMBOL

# Weight per node type
_TYPE_WEIGHT = {FILE: 10, SYMBOL: 8, TURN: 6, "CALL": 4, "TASK": 5}

# Keywords that map to domain tags
_KEYWORD_TAG_MAP = {
    "auth": "auth", "login": "auth", "token": "auth", "password": "auth",
    "secur": "security", "encrypt": "security", "jwt": "auth",
    "database": "database", "db": "database", "sql": "database", "schema": "database",
    "api": "api", "endpoint": "api", "route": "api",
    "test": "test", "spec": "test",
    "config": "config", "settings": "config",
    "deploy": "infra", "docker": "infra",
    "component": "ui", "page": "ui", "view": "ui",
}


def _prompt_tags(prompt: str) -> set[str]:
    tags = set()
    lower = prompt.lower()
    for kw, tag in _KEYWORD_TAG_MAP.items():
        if kw in lower:
            tags.add(tag)
    return tags


def _score_node(node: Node, prompt_tags: set[str], recent_ids: set[str]) -> float:
    score = 0.0
    # Tag overlap
    overlap = set(node.tags) & prompt_tags
    score += len(overlap) * 20
    # Node type base weight
    score += _TYPE_WEIGHT.get(node.type, 3)
    # Recency bonus
    if node.id in recent_ids:
        score += 15
    # Complexity surfacing — complex nodes rise for complex prompts
    if prompt_tags:
        score += node.complexity * 0.1
    return score


def query(graph: ContextGraph, prompt: str, k: int = 5) -> List[Node]:
    """Return k most relevant nodes for the given prompt."""
    if not graph or len(graph) == 0:
        return []

    tags    = _prompt_tags(prompt)
    recent  = set(graph.recent_ids(10))
    nodes   = graph.all_nodes()

    scored = [(n, _score_node(n, tags, recent)) for n in nodes]
    scored.sort(key=lambda x: x[1], reverse=True)
    return [n for n, s in scored[:k] if s > 0]


def complexity_floor(graph: ContextGraph, prompt: str) -> int:
    """Return a complexity floor based on relevant context nodes.

    Higher when the relevant nodes are complex files / active tasks.
    """
    nodes = query(graph, prompt, k=4)
    if not nodes:
        return 0
    top = max(n.complexity for n in nodes)
    avg = sum(n.complexity for n in nodes) // len(nodes)
    # Floor = average of top and mean, capped at 70
    # (we don't let context alone push to opus — message still matters)
    return min(70, (top + avg) // 2)
