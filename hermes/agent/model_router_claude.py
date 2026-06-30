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
_TRIVIAL_SCOPE = re.compile(
    r"\b(just|only|quick|simple|small|one|single|that one|this one|the one|"
    r"that specific|just that|just this)\b",
    re.IGNORECASE,
)
_MICRO_TASKS = re.compile(
    r"\b(typo|rename|comment|import|semicolon|bracket|whitespace|format|"
    r"indent|spelling|log statement|print statement)\b",
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
    hits = len(_HIGH_SIGNALS.findall(msg))
    score += min(30, hits * 8)
    if _HAS_CODE.search(msg):
        score += 15
    score += min(10, msg.count("?") * 4)
    if _CONTEXT_REF.search(msg) and len(msg) < 80:
        score += 10
    score += min(10, len(history) // 3)
    score += min(15, sum(1 for m in history[-6:] if m.get("role") == "tool") * 5)

    # Trivial scope reduction
    if _TRIVIAL_SCOPE.search(msg):
        score -= 15
    if _MICRO_TASKS.search(msg):
        score -= 20
    if hits > 0 and len(msg) < 60:
        score -= 10

    return max(0, min(100, score))


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
    """Stateful router — one instance per session/conversation.

    If a context graph is attached (via set_context_graph), the semantic
    complexity floor from the context tree is merged with the session floor.
    """

    def __init__(self, tiers: Optional[ClaudeTiers] = None):
        self.tiers = tiers or ClaudeTiers()
        self._session = _Session()
        self._context_graph = None

    def set_context_graph(self, graph: Any) -> None:
        """Attach a ContextGraph instance for semantic floor boosting."""
        self._context_graph = graph

    def route(
        self,
        message: str,
        history: Optional[List[dict]] = None,
        call_type: str = "plan",
        user_message: Any = None,
    ) -> str:
        """Return the Claude model name for this call.

        Uses the complexity matrix (4-dimensional scoring) when available,
        falls back to the legacy single-score algorithm.
        """
        history = history or []

        # ── Context tree floor ────────────────────────────────────────────────
        ctx_floor_val = 0
        if self._context_graph is not None and len(self._context_graph) > 0:
            try:
                try:
                    from agent.context_tree import complexity_floor as _ctx_floor
                except ImportError:
                    from hermes.agent.context_tree import complexity_floor as _ctx_floor
                ctx_floor_val = _ctx_floor(self._context_graph, message)
            except Exception:
                pass

        # ── Feedback floor ────────────────────────────────────────────────────
        fb_floor = 0
        try:
            fb_floor = self._session.feedback_floor(call_type) if hasattr(self._session, 'feedback_floor') else 0
        except Exception:
            pass

        # ── Complexity matrix scoring (preferred) ─────────────────────────────
        offset = CALL_TYPE_OFFSET.get(call_type, 0)
        try:
            try:
                from agent.complexity_matrix import evaluate, select_model as _matrix_select
            except ImportError:
                from hermes.agent.complexity_matrix import evaluate, select_model as _matrix_select

            dims = evaluate(
                text=message,
                messages=history,
                history=history,
                user_message=user_message,
                ctx_tree_floor=max(ctx_floor_val, self._session.complexity_floor),
                feedback_floor=fb_floor,
            )

            # Map matrix dimensions → Claude tier
            # Combined score is weighted sum; apply offset then floor boosts
            combined = dims.combined + offset
            combined = max(0, min(100, combined))
            combined = max(combined, ctx_floor_val, self._session.complexity_floor)

            # Priority 1 — Vision
            if dims.vision >= 100:
                # Haiku has basic vision; Sonnet for anything with real text
                model = self.tiers.tier2 if dims.text <= 30 else self.tiers.tier3
            # Priority 2 — Heavy code (multi-file, complex codebase)
            elif dims.code >= 50:
                model = self.tiers.tier3
            elif dims.code > 25 and dims.code >= dims.text * 0.5:
                model = self.tiers.tier2
            # Priority 3 — Text complexity (architecture/design prompts with no code)
            # text >= 55 means 3+ high-signal keywords → needs at least Sonnet
            elif dims.text >= 55:
                model = self.tiers.tier2 if dims.text < 75 else self.tiers.tier3
            # Priority 4 — Context depth (mid-task tool loops)
            # High context (many tool results in flight) needs Sonnet minimum
            elif dims.context >= 30:
                model = self.tiers.tier2
            # Priority 5 — Standard combined score
            # Claude-specific thresholds (tighter than Ollama — Haiku is strong)
            elif combined <= 15:
                model = self.tiers.tier0   # trivial: "ok", "thanks", "yes"
            elif combined <= 35:
                model = self.tiers.tier1   # simple Q&A, short follow-ups
            elif combined <= 60:
                model = self.tiers.tier2   # analysis, medium tasks
            else:
                model = self.tiers.tier3   # architecture, deep reasoning

            logger.debug(
                "claude_router[matrix]: call_type=%s text=%d code=%d vision=%d "
                "ctx=%d combined=%.1f floor=%d ctx_floor=%d → %s",
                call_type, dims.text, dims.code, dims.vision, dims.context,
                dims.combined, self._session.complexity_floor, ctx_floor_val, model,
            )
            return model

        except Exception as exc:
            logger.debug("claude_router: matrix unavailable (%s), falling back", exc)

        # ── Fallback: legacy single-score ─────────────────────────────────────
        turn_score = _score(message, history)
        effective = max(turn_score, self._session.complexity_floor, ctx_floor_val)
        final = max(0, min(100, effective + offset))
        model = self.tiers.for_score(final)

        logger.debug(
            "claude_router[legacy]: call_type=%s score=%d floor=%d ctx=%d final=%d → %s",
            call_type, turn_score, self._session.complexity_floor, ctx_floor_val, final, model,
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
    If agent._context_graph exists, attaches it to the router for
    semantic floor boosting.
    Returns None when HERMES_MODEL_ROUTER=0.
    """
    if os.getenv("HERMES_MODEL_ROUTER", "1") == "0":
        return None

    router: ClaudeRouter = getattr(agent, "_claude_router", None)
    if router is None:
        router = ClaudeRouter(tiers or _DEFAULT_TIERS)
        agent._claude_router = router

    # Wire context graph if available
    graph = getattr(agent, "_context_graph", None)
    if graph is not None:
        router.set_context_graph(graph)

    return router.route(user_message, history or [], call_type="plan")
