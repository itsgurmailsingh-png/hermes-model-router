"""Model router for Hermes — Ollama Cloud tier.

Routes each LLM call to the cheapest model tier that can handle it.

Tiers (configure via HERMES_ROUTER_* env vars or pass ModelTiers to init):
  0  → ministral-3:3b        (greetings, lookups, title gen)
  1  → gemma3:12b            (simple Q&A, summarise, verify)
  2  → devstral-small-2:24b  (coding, analysis, structured output)
  3  → glm-5.2               (planning, architecture, multi-file refactors)

All four are verified available on Ollama Cloud (/api/tags, 2026-06-27).

Call types and their tier offsets:
  PLAN      +0   (uses full session score)
  ANALYZE   -1   (one step down from plan)
  CODEGEN   -1
  VERIFY    -2
  SUMMARIZE -3   (always cheap)
  TITLE     -99  (always tier 0)
  SUBAGENT  -1   (children run one tier below parent)

Usage in conversation_loop.py — add near top of run_conversation():

    from agent.model_router import route_turn
    routed = route_turn(agent, user_message, conversation_history or [])
    if routed:
        agent._router_original_model = agent.model
        agent.model = routed

And restore after the turn if you want (optional — next turn re-routes anyway).
"""

from __future__ import annotations

import os
import re
import logging
from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Any, List, Optional

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Call types
# ---------------------------------------------------------------------------

class CallType(str, Enum):
    PLAN      = "plan"       # Top-level turn: understand + orchestrate
    ANALYZE   = "analyze"    # Examine tool output / read files
    CODEGEN   = "codegen"    # Write / edit code
    VERIFY    = "verify"     # Check correctness, lint, review
    SUMMARIZE = "summarize"  # Compress, recap, format
    TITLE     = "title"      # Generate a session/chat title
    SUBAGENT  = "subagent"   # Spawned child agent


CALL_TYPE_OFFSET: dict[CallType, int] = {
    CallType.PLAN:      0,
    CallType.ANALYZE:  -10,
    CallType.CODEGEN:  -10,
    CallType.VERIFY:   -20,
    CallType.SUMMARIZE:-30,
    CallType.TITLE:    -99,
    CallType.SUBAGENT: -10,
}


# ---------------------------------------------------------------------------
# Model tiers  (defaults = sensible Ollama Cloud choices)
# ---------------------------------------------------------------------------

@dataclass
class ModelTiers:
    """Maps score ranges to model names.

    Override via env vars:
      HERMES_ROUTER_TIER0=qwen2.5:1.5b
      HERMES_ROUTER_TIER1=qwen2.5:7b
      HERMES_ROUTER_TIER2=qwen2.5:32b
      HERMES_ROUTER_TIER3=qwen2.5:72b
    """
    tier0: str = ""   # resolved at runtime
    tier1: str = ""
    tier2: str = ""
    tier3: str = ""

    def __post_init__(self):
        # Defaults are actual Ollama Cloud models (verified against /api/tags).
        # Tier 0 → ministral-3:3b   (4.7 GB  — greetings, titles, lookups)
        # Tier 1 → gemma3:12b       (24 GB   — simple Q&A, short answers)
        # Tier 2 → devstral-small-2:24b (51.6 GB — coding, analysis)
        # Tier 3 → glm-5.2          (heavy   — planning, architecture)
        self.tier0 = self.tier0 or os.getenv("HERMES_ROUTER_TIER0", "ministral-3:3b")
        self.tier1 = self.tier1 or os.getenv("HERMES_ROUTER_TIER1", "gemma3:12b")
        self.tier2 = self.tier2 or os.getenv("HERMES_ROUTER_TIER2", "devstral-small-2:24b")
        self.tier3 = self.tier3 or os.getenv("HERMES_ROUTER_TIER3", "glm-5.2")

    def for_score(self, score: int) -> str:
        if score <= 20:
            return self.tier0
        if score <= 45:
            return self.tier1
        if score <= 70:
            return self.tier2
        return self.tier3


# ---------------------------------------------------------------------------
# Session score  (lives on agent._router_session)
# ---------------------------------------------------------------------------

@dataclass
class RouterSession:
    """Lightweight per-session routing state attached to the agent object."""
    complexity_floor: int = 0      # Never route below this within the session
    tools_used: set[str] = field(default_factory=set)
    files_touched: int = 0
    turns: int = 0
    last_score: int = 0
    # Feedback loop: track failures per call_type to adapt routing
    # {call_type_str: {"fails": int, "total": int, "floor_boost": int}}
    feedback: dict[str, dict] = field(default_factory=dict)

    def update(self, turn_score: int, tools: list[str], files_written: int):
        self.turns += 1
        self.last_score = turn_score
        self.tools_used.update(tools)
        self.files_touched += files_written
        # Floor rises when the session is getting complex; decays slowly
        if turn_score > self.complexity_floor:
            self.complexity_floor = turn_score
        else:
            # Decay: after a simple turn the floor drops by 5 (not instantly)
            self.complexity_floor = max(0, self.complexity_floor - 5)

    def record_result(self, call_type: str, success: bool) -> None:
        """Record whether a routed call succeeded or failed.

        On failure, boosts the floor for that call_type so the router
        won't make the same mistake. On success, gradually decays the boost.
        """
        ct = call_type or "plan"
        entry = self.feedback.setdefault(ct, {"fails": 0, "total": 0, "floor_boost": 0})
        entry["total"] += 1
        if not success:
            entry["fails"] += 1
            # Each failure adds 10 to the floor boost (caps at 30)
            entry["floor_boost"] = min(30, entry["floor_boost"] + 10)
        else:
            # Each success decays the boost by 3
            entry["floor_boost"] = max(0, entry["floor_boost"] - 3)

    def feedback_floor(self, call_type: str) -> int:
        """Return the feedback-derived floor boost for a call type."""
        entry = self.feedback.get(call_type or "plan")
        if not entry:
            return 0
        return entry.get("floor_boost", 0)


# ---------------------------------------------------------------------------
# Scoring  (Layer 0 + Layer 1 — no LLM needed)
# ---------------------------------------------------------------------------

# Patterns that push score UP
_HIGH_SIGNALS = re.compile(
    r"\b(refactor|rewrite|architect|implement|migrate|debug|analyse|analyze|"
    r"design|optimize|secur|authent|deploy|integrat|build|create|explain why|"
    r"why does|how does|compare|review|audit|test suite|end.to.end|pipeline|"
    r"multi.file|codebase|system|database|schema|api|endpoint|algorithm)\b",
    re.IGNORECASE,
)

# Patterns that push score DOWN
_LOW_SIGNALS = re.compile(
    r"^\s*(hi|hello|hey|thanks|thank you|ok|okay|sure|yes|no|great|done|"
    r"got it|sounds good|perfect|nice|cool)\s*[!.]?\s*$",
    re.IGNORECASE,
)

# Trivial scope reducers — "just", "only", "one", "quick" + a single noun
# These signal the task is small even if a keyword like "refactor" is present
_TRIVIAL_SCOPE = re.compile(
    r"\b(just|only|quick|simple|small|one|single|that one|this one|the one|"
    r"that specific|just that|just this)\b",
    re.IGNORECASE,
)

# "fix the typo", "rename this variable", "add a comment" — micro-tasks
_MICRO_TASKS = re.compile(
    r"\b(typo|rename|comment|import|semicolon|bracket|whitespace|format|"
    r"indent|spelling|log statement|print statement)\b",
    re.IGNORECASE,
)

# Code block in the message (user pasted code → complexity)
_HAS_CODE = re.compile(r"```|\bdef \b|\bclass \b|\bfunction\b|\bimport \b", re.IGNORECASE)

# Conversational reference to previous turn ("it", "that", "the above")
_CONTEXT_REF = re.compile(r"\b(it|that|this|the above|the previous|as before|same as)\b", re.IGNORECASE)


def _score_message(message: str, history: List[dict]) -> int:
    """Return 0-100 complexity score for a single message + recent history."""
    msg = message or ""

    # Immediate low-signal override
    if _LOW_SIGNALS.match(msg):
        return 5

    score = 30  # baseline

    # Message length (longer = more complex, up to +20)
    length = len(msg)
    score += min(20, length // 40)

    # High-signal keyword hits (+8 each, cap at +30)
    hits = len(_HIGH_SIGNALS.findall(msg))
    score += min(30, hits * 8)

    # Code in message
    if _HAS_CODE.search(msg):
        score += 15

    # Multi-sentence (question count)
    questions = msg.count("?")
    score += min(10, questions * 4)

    # Context reference without much content = ambiguous but session-dependent
    if _CONTEXT_REF.search(msg) and length < 80:
        score += 10  # relies on history → complexity floor will handle it

    # History depth (more turns in flight = more context dependency)
    depth = len(history)
    score += min(10, depth // 3)

    # Tool results in recent history (agent is mid-task)
    tool_msgs = sum(1 for m in history[-6:] if m.get("role") == "tool")
    score += min(15, tool_msgs * 5)

    # ── Trivial scope reduction ────────────────────────────────────────────
    # If the message contains "just", "only", "one", "quick" etc.,
    # the task is small even if keywords are present.
    if _TRIVIAL_SCOPE.search(msg):
        score -= 15

    # Micro-tasks: typo, rename, comment, format, spelling → always cheap
    if _MICRO_TASKS.search(msg):
        score -= 20

    # Short message with a keyword but no context = probably trivial
    # "refactor this" (14 chars) shouldn't score the same as a 500-char
    # refactor plan even though both have "refactor"
    if hits > 0 and length < 60:
        score -= 10  # keyword present but message is too short to be serious

    return max(0, min(100, score))


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

_DEFAULT_TIERS = ModelTiers()

_TIER_OVERRIDE = re.compile(r"^\s*/t([0-3])\b", re.IGNORECASE)


def _parse_tier_override(message: str) -> Optional[int]:
    """Check if the message starts with /t0, /t1, /t2, or /t3.
    Returns the tier number (0-3) or None.
    """
    m = _TIER_OVERRIDE.match(message or "")
    if m:
        return int(m.group(1))
    return None


def route_call(
    agent: Any,
    call_type: CallType = CallType.PLAN,
    message: str = "",
    history: Optional[List[dict]] = None,
    tiers: Optional[ModelTiers] = None,
) -> str:
    """Return the model name to use for this call.

    Attaches/updates a RouterSession on agent._router_session.
    Does NOT mutate agent.model — caller decides whether to swap.

    If agent._context_graph exists (from ContextTreeBuilder), the context
    tree's semantic complexity floor is merged with the session floor —
    giving a floor that reflects what files you've been touching, not just
    the last message score.

    Manual override: prefix the message with /t0, /t1, /t2, or /t3 to
    force a specific tier for this call (bypasses all scoring).
    """
    tiers = tiers or _DEFAULT_TIERS
    history = history or []

    # ── Manual override ────────────────────────────────────────────────────
    # /t0 /t1 /t2 /t3 — force tier for this one call
    msg = message or ""
    override = _parse_tier_override(msg)
    if override is not None:
        model = [tiers.tier0, tiers.tier1, tiers.tier2, tiers.tier3][override]
        logger.debug("model_router: MANUAL OVERRIDE → tier %d → %s", override, model)
        return model

    # Get or create session state
    session: RouterSession = getattr(agent, "_router_session", None)
    if session is None:
        session = RouterSession()
        agent._router_session = session

    # Load persisted feedback on first call
    if not session.feedback and session.turns == 0:
        try:
            persisted = load_feedback()
            if persisted:
                session.feedback = persisted
        except Exception:
            pass

    # Score the message
    turn_score = _score_message(message, history)

    # Apply session complexity floor
    effective_score = max(turn_score, session.complexity_floor)

    # Apply feedback floor (if this call_type failed before, boost the floor)
    feedback_boost = session.feedback_floor(call_type.value)
    if feedback_boost > 0:
        effective_score = max(effective_score, turn_score + feedback_boost)
        effective_score = min(100, effective_score)

    # Boost floor from context tree if available
    ctx_floor_val = 0
    graph = getattr(agent, "_context_graph", None)
    if graph is not None and len(graph) > 0:
        try:
            try:
                from agent.context_tree import complexity_floor as _ctx_floor
            except ImportError:
                from hermes.agent.context_tree import complexity_floor as _ctx_floor
            ctx_floor_val = _ctx_floor(graph, message)
            effective_score = max(effective_score, ctx_floor_val)
        except Exception:
            pass

    # Apply call-type offset (planning gets full score; summarise always cheap)
    offset = CALL_TYPE_OFFSET.get(call_type, 0)
    final_score = max(0, min(100, effective_score + offset))

    # Resolve tier
    model = tiers.for_score(final_score)

    logger.debug(
        "model_router: call_type=%s msg_score=%d floor=%d feedback=%d ctx_floor=%d "
        "effective=%d final=%d → %s",
        call_type.value, turn_score, session.complexity_floor,
        feedback_boost, ctx_floor_val, effective_score, final_score, model,
    )

    return model


def route_turn(
    agent: Any,
    user_message: str,
    history: Optional[List[dict]] = None,
    tiers: Optional[ModelTiers] = None,
) -> Optional[str]:
    """Convenience wrapper for the top-level turn (CallType.PLAN).

    Returns None when routing is disabled (HERMES_MODEL_ROUTER=0) so the
    caller can skip the swap cleanly:

        routed = route_turn(agent, message, history)
        if routed:
            agent.model = routed
    """
    if os.getenv("HERMES_MODEL_ROUTER", "1") == "0":
        return None

    return route_call(agent, CallType.PLAN, user_message, history, tiers)


def update_session_after_turn(
    agent: Any,
    turn_score: int,
    tools_used: Optional[list] = None,
    files_written: int = 0,
):
    """Call this at the end of a turn to update the session floor.

    tools_used: list of tool names invoked this turn (from agent._tool_call_log or similar)
    files_written: number of files written/edited this turn
    """
    session: RouterSession = getattr(agent, "_router_session", None)
    if session is None:
        return
    session.update(turn_score, tools_used or [], files_written)


def record_call_result(
    agent: Any,
    call_type: str,
    success: bool,
):
    """Record whether a routed call succeeded or failed.

    Call this after the LLM response is processed. If the model produced
    a good result (no errors, user didn't retry), call with success=True.
    If the model failed (hallucinated, wrong code, user retried), call
    with success=False. The router will boost the floor for that call_type
    so future calls of the same type route to a higher tier.

    Example:
        from agent.model_router import record_call_result
        record_call_result(agent, "codegen", success=True)
        record_call_result(agent, "analyze", success=False)
    """
    session: RouterSession = getattr(agent, "_router_session", None)
    if session is None:
        return
    session.record_result(call_type, success)


# ── Feedback persistence ──────────────────────────────────────────────────────

_FEEDBACK_FILE = Path.home() / ".hermes" / "router-logs" / "feedback.json"


def save_feedback(agent: Any) -> None:
    """Persist feedback data so it survives across sessions."""
    session: RouterSession = getattr(agent, "_router_session", None)
    if session is None or not session.feedback:
        return
    try:
        import json
        _FEEDBACK_FILE.parent.mkdir(parents=True, exist_ok=True)
        existing = {}
        if _FEEDBACK_FILE.exists():
            existing = json.loads(_FEEDBACK_FILE.read_text())
        for ct, entry in session.feedback.items():
            if ct in existing:
                existing[ct]["fails"] += entry["fails"]
                existing[ct]["total"] += entry["total"]
                existing[ct]["floor_boost"] = max(
                    existing[ct].get("floor_boost", 0),
                    entry.get("floor_boost", 0),
                )
            else:
                existing[ct] = entry
        _FEEDBACK_FILE.write_text(json.dumps(existing, indent=2))
    except Exception:
        pass


def load_feedback() -> dict[str, dict]:
    """Load persisted feedback data."""
    try:
        import json
        if _FEEDBACK_FILE.exists():
            return json.loads(_FEEDBACK_FILE.read_text())
    except Exception:
        pass
    return {}
