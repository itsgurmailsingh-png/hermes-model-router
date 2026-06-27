"""Context tree graph — nodes, edges, in-memory store, persistence."""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional

_PERSIST_PATH = Path.home() / ".hermes" / "router-logs" / "context-graph.json"

# Node types
FILE     = "FILE"
SYMBOL   = "SYMBOL"
TURN     = "TURN"
CALL     = "CALL"
TASK     = "TASK"

# Edge types
IMPORTS      = "IMPORTS"
CALLS        = "CALLS"
MODIFIED_BY  = "MODIFIED_BY"
TRIGGERED    = "TRIGGERED"
DEPENDS_ON   = "DEPENDS_ON"
REFERENCED   = "REFERENCED"


@dataclass
class Node:
    id:         str
    type:       str
    label:      str
    tags:       List[str]   = field(default_factory=list)
    complexity: int         = 0
    ts:         str         = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())
    meta:       Dict        = field(default_factory=dict)

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Node":
        return cls(**d)


@dataclass
class Edge:
    src:   str
    dst:   str
    type:  str
    ts:    str = field(default_factory=lambda: datetime.now(timezone.utc).isoformat())

    def to_dict(self) -> dict:
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict) -> "Edge":
        return cls(**d)


class ContextGraph:
    """Thread-safe in-memory context graph with optional persistence."""

    def __init__(self, session_id: str = ""):
        self._lock   = threading.Lock()
        self.session = session_id
        self._nodes: Dict[str, Node] = {}
        self._edges: List[Edge]      = []
        # recency: list of node ids in touch order
        self._recent: List[str]      = []

    # ── Nodes ────────────────────────────────────────────────────────────────

    def add_node(self, node: Node) -> None:
        with self._lock:
            existing = self._nodes.get(node.id)
            if existing:
                # Merge: keep highest complexity, union tags
                node.complexity = max(node.complexity, existing.complexity)
                node.tags = list(set(node.tags) | set(existing.tags))
            self._nodes[node.id] = node
            self._touch(node.id)

    def get_node(self, node_id: str) -> Optional[Node]:
        return self._nodes.get(node_id)

    def all_nodes(self) -> List[Node]:
        return list(self._nodes.values())

    # ── Edges ────────────────────────────────────────────────────────────────

    def add_edge(self, src: str, dst: str, etype: str) -> None:
        with self._lock:
            for e in self._edges:
                if e.src == src and e.dst == dst and e.type == etype:
                    return  # deduplicate
            self._edges.append(Edge(src=src, dst=dst, type=etype))

    def neighbors(self, node_id: str) -> List[Node]:
        """Return all nodes directly connected to node_id."""
        ids = set()
        for e in self._edges:
            if e.src == node_id:
                ids.add(e.dst)
            elif e.dst == node_id:
                ids.add(e.src)
        return [self._nodes[i] for i in ids if i in self._nodes]

    # ── Recency ──────────────────────────────────────────────────────────────

    def _touch(self, node_id: str) -> None:
        if node_id in self._recent:
            self._recent.remove(node_id)
        self._recent.append(node_id)
        if len(self._recent) > 100:
            self._recent = self._recent[-100:]

    def recent_ids(self, n: int = 10) -> List[str]:
        return self._recent[-n:]

    # ── Persistence ──────────────────────────────────────────────────────────

    def save(self, path: Path = _PERSIST_PATH) -> None:
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            with self._lock:
                data = {
                    "session": self.session,
                    "nodes":   [n.to_dict() for n in self._nodes.values()],
                    "edges":   [e.to_dict() for e in self._edges],
                    "recent":  self._recent,
                }
            path.write_text(json.dumps(data, indent=2))
        except Exception:
            pass

    @classmethod
    def load(cls, path: Path = _PERSIST_PATH, session_id: str = "") -> "ContextGraph":
        g = cls(session_id=session_id)
        try:
            if path.exists():
                data = json.loads(path.read_text())
                for nd in data.get("nodes", []):
                    g._nodes[nd["id"]] = Node.from_dict(nd)
                for ed in data.get("edges", []):
                    g._edges.append(Edge.from_dict(ed))
                g._recent = data.get("recent", [])
        except Exception:
            pass
        return g

    def __len__(self) -> int:
        return len(self._nodes)
