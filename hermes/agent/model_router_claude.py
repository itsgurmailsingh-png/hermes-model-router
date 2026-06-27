"""Model router — Claude / Anthropic tier.

Same logic as model_router.py but maps to Claude model names.
Use this if your Hermes provider is anthropic / openrouter pointing at Claude,
or as a standalone router for any Claude Code / Claude API integration.

Tiers:
  0  → claude-haiku-4-5-20251001     (greetings, titles, summaries)
  1  → claude-haiku-4-5-20251001     (simple tasks, short answers)
  2  → claude-sonnet-4-6             (coding, analysis, structured output)
  3  → claude-opus-4-6               (planning, architecture, multi-file work)

Override via env vars:
  CLAUDE_ROUTER_TIER0=claude-haiku-4-5-20251001
  CLAUDE_ROUTER_TIER1=claude-haiku-4-5-20251001
  CLAUDE_ROUTER_TIER2=claude-sonnet-4-6
  CLAUDE_ROUTER_TIER3=claude-opus-4-6

Usage (standalone / Claude API):

    from agent.model_router_claude import ClaudeRouter

    router = ClaudeRouter()
    model = router.route(message, history)
    # → "claude-haiku-4-5-20251001" | "claude-sonnet-4-6" | "claude-opus-4-6"

Usage (Hermes with anthropic provider):

    from agent.model_router_claude import route_turn_claude
    routed = route_turn_claude(agent, user_message, history)
    if routed:
        agent.model = routed
"""

from __future__ import annotations

import os
import re
import logging
from dataclasses import dataclass, field
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Model tiers
# ---------------------------------------------------------------------------

@dataclass
class ClaudeTiers:
    tier0: str = ""
    tier1: str = ""
    tier2: str = ""
    tier3: str = ""

    def __post_init__(self):
        self.tier0 = self.tier0 or os.getenv(
            "CLAUDE_ROUTER_TIER0", "claude-haiku-4-5-20251001"
        )
        self.tier1 = self.tier1 or os.getenv(
            "CLAUDE_ROUTER_TIER1", "claude-haiku-4-5-20251001"
        )
        self.tier2 = self.tier2 or os.getenv(
            "CLAUDE_ROUTER_TIER2", "claude-sonnet-4-6"
        )
        self.tier3 = self.tier3 or os.getenv(
            "CLAUDE_ROUTER_TIER3", "claude-opus-4-6"
        )

    def for_score(self, score: int) -> str:
        if score <= 20:
            return self.tier0
        if score <= 45:
            return self.tier1
        if score <= 70:
            return self.tier2
        return self.tier3


# ---------------------------------------------------------------------------
# Call type offsets  (same logic as model_router.py)
# ---------------------------------------------------------------------------

CALL_TYPE_OFFSET = {
    "plan":       0,
    "analyze":   -10,
    "codegen":   -10,
    "verify":    -20,
    "summarize": -30,
    "title":     -99,
    "subagent":  -10,
}


# ---------------------------------------------------------------------------
# Scoring  (identical algorithm to model_router.py)
# ---------------------------------------------------------------------------

_HIGH_SIGNALS = re.compile(
    r"\b(refactor|rewrite|architect|implement|migrate|debug|analyse|analyze|"
    r"design|optimize|secur|authent|deploy|integrat|build|create|explain why|"
    r"why does|how does|compare|review|audit|test suite|end.to.end|pipeline|"
    r"multi.file|codebase|system|database|schema|api|endpoint|algorithm)\b",
    re.IGNORECASE,
)
_LOW_SIGNALS = re.compile(
    r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|sure|yes|no|great|done|"
    r"got it|sounds good|perfect|nice|cool)\s*[!.]?\s*$",
    re.IGNORECASE,
)
_HAS_CODE = re.compile(r"```|\bdef \b|\bclass \b|\bfunction\b|\bimport \b", re.IGNORECASE)
_CONTEXT_REF = re.compile(r"\b(it|that|this|the above|the previous|as before|same as)\b", re.IGNORECASE)


def _score(message: str, history: List[dict]) -> int:
    msg = message or ""
    if _LOW_SIGNALS.match(msg):
        return 5

    score = 30
    score += min(20, len(msg) // 40)
    score += min(30, len(_HIGH_SIGNALS.findall(msg)) * 8)
    if _HAS_CODE.search(msg):
        score += 15
    score += min(10, msg.count("?") * 4)
    if _CONTEXT_REF.search(msg) and len(msg) < 80:
        score += 10
    score += min(10, len(history) // 3)
    score += min(15, sum(1 for m in history[-6:] if m.get("role") == "tool") * 5)
    return min(100, score)


# ---------------------------------------------------------------------------
# Session state
# ---------------------------------------------------------------------------

@dataclass
class _Session:
    complexity_floor: int = 0
    turns: int = 0

    def update(self, score: int):
        self.turns += 1
        if score > self.complexity_floor:
            self.complexity_floor = score
        else:
            self.complexity_floor = max(0, self.complexity_floor - 5)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

class ClaudeRouter:
    """Stateful router — one instance per session/conversation."""

    def __init__(self, tiers: Optional[ClaudeTiers] = None):
        self.tiers = tiers or ClaudeTiers()
        self._session = _Session()

    def route(
        self,
        message: str,
        history: Optional[List[dict]] = None,
        call_type: str = "plan",
    ) -> str:
        """Return the Claude model name for this call."""
        history = history or []
        turn_score = _score(message, history)
        effective = max(turn_score, self._session.complexity_floor)
        offset = CALL_TYPE_OFFSET.get(call_type, 0)
        final = max(0, min(100, effective + offset))
        model = self.tiers.for_score(final)

        logger.debug(
            "claude_router: call_type=%s msg_score=%d floor=%d final=%d → %s",
            call_type, turn_score, self._session.complexity_floor, final, model,
        )
        return model

    def end_turn(self, score: Optional[int] = None, message: str = "", history: Optional[List[dict]] = None):
        """Call after each turn to update session floor."""
        if score is None:
            score = _score(message, history or [])
        self._session.update(score)

    @property
    def complexity_floor(self) -> int:
        return self._session.complexity_floor


# ---------------------------------------------------------------------------
# Hermes-style convenience wrappers (mirrors model_router.py interface)
# ---------------------------------------------------------------------------

_DEFAULT_TIERS = ClaudeTiers()


def route_turn_claude(
    agent: Any,
    user_message: str,
    history: Optional[List[dict]] = None,
    tiers: Optional[ClaudeTiers] = None,
) -> Optional[str]:
    """Drop-in replacement for route_turn() when provider is anthropic.

    Attaches a ClaudeRouter to agent._claude_router on first call.
    Returns None when HERMES_MODEL_ROUTER=0.
    """
    if os.getenv("HERMES_MODEL_ROUTER", "1") == "0":
        return None

    router: ClaudeRouter = getattr(agent, "_claude_router", None)
    if router is None:
        router = ClaudeRouter(tiers or _DEFAULT_TIERS)
        agent._claude_router = router

    return router.route(user_message, history or [], call_type="plan")
