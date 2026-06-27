"""Context tree — live semantic graph of the agent's working context."""

from __future__ import annotations

import sys

# Support both "agent.context_tree" (Hermes internal) and
# "hermes.agent.context_tree" (standalone / testing) import styles
try:
    from agent.context_tree.graph import ContextGraph, Node, Edge
    from agent.context_tree.builder import ContextTreeBuilder
    from agent.context_tree.query import query, complexity_floor
except ImportError:
    from .graph import ContextGraph, Node, Edge
    from .builder import ContextTreeBuilder
    from .query import query, complexity_floor

__all__ = [
    "ContextGraph", "Node", "Edge",
    "ContextTreeBuilder",
    "query", "complexity_floor",
]
