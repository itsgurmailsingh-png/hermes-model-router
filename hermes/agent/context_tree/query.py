"""Context tree query — returns relevant nodes for a prompt."""

from __future__ import annotations

import re
import json
from pathlib import Path
from typing import List

try:
    from agent.context_tree.graph import ContextGraph, Node, FILE, TURN, SYMBOL
except ImportError:
    from .graph import ContextGraph, Node, FILE, TURN, SYMBOL

# Weight per node type
_TYPE_WEIGHT = {FILE: 10, SYMBOL: 8, TURN: 6, "CALL": 4, "TASK": 5}

# ── Keyword→tag map (loaded from tags.json, same as builder) ──────────────────
_KEYWORD_TAG_MAP: dict[str, str] | None = None
_TAGS_FILE = Path(__file__).parent / "tags.json"


def _load_keyword_map() -> dict[str, str]:
    """Build keyword→tag map from tags.json."""
    global _KEYWORD_TAG_MAP
    if _KEYWORD_TAG_MAP is not None:
        return _KEYWORD_TAG_MAP

    mapping: dict[str, str] = {}
    try:
        if _TAGS_FILE.exists():
            data = json.loads(_TAGS_FILE.read_text())
            for tag, regexes in data.items():
                if tag.startswith("_"):
                    continue
                if isinstance(regexes, list):
                    for rx in regexes:
                        # Use the first word of each regex as a simple keyword
                        # (more complex patterns still match in builder)
                        clean = rx.replace("\\.", "").replace("(", "").replace(")", "")
                        for word in clean.split("|"):
                            word = word.strip()
                            if word and len(word) > 1:
                                mapping[word.lower()] = tag
    except Exception:
        pass

    if not mapping:
        # Hardcoded fallback
        mapping = {
            "auth": "auth", "login": "auth", "token": "auth", "password": "auth",
            "secur": "security", "encrypt": "security", "jwt": "auth",
            "database": "database", "db": "database", "sql": "database", "schema": "database",
            "api": "api", "endpoint": "api", "route": "api",
            "test": "test", "spec": "test",
            "config": "config", "settings": "config",
            "deploy": "infra", "docker": "infra",
            "component": "ui", "page": "ui", "view": "ui",
        }

    _KEYWORD_TAG_MAP = mapping
    return mapping


def _prompt_tags(prompt: str) -> set[str]:
    tags = set()
    lower = prompt.lower()
    keyword_map = _load_keyword_map()
    for kw, tag in keyword_map.items():
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


def _decayed_complexity(node: Node, recent_ids: list[str]) -> float:
    """Return complexity with recency decay applied.

    recent_ids is ordered oldest→newst (last touched = end of list).
    Files near the end (recently touched) get full complexity.
    Files near the start (touched long ago) get decayed complexity.
    """
    base = float(node.complexity)
    if not recent_ids:
        return base
    try:
        idx = recent_ids.index(node.id)
    except ValueError:
        return base * 0.4  # not in recency list at all
    # Distance from the end (most recent)
    dist_from_end = len(recent_ids) - idx
    if dist_from_end <= 10:
        return base          # very recent
    elif dist_from_end <= 30:
        return base * 0.8    # somewhat recent
    elif dist_from_end <= 50:
        return base * 0.6    # getting old
    else:
        return base * 0.4    # stale


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
    Applies recency decay — files not touched recently contribute less.
    """
    nodes = query(graph, prompt, k=4)
    if not nodes:
        return 0
    recent = graph.recent_ids(100)  # full recency list for decay calc
    decayed = [_decayed_complexity(n, recent) for n in nodes]
    top = max(decayed)
    avg = sum(decayed) / len(decayed)
    # Floor = average of top and mean, capped at configurable max
    cap = 70
    try:
        import os as _os
        cap = int(_os.getenv("HERMES_ROUTER_CTX_FLOOR_CAP", "70"))
    except Exception:
        pass
    return min(cap, int((top + avg) / 2))
