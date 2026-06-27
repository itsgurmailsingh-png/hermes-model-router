"""model-router — Hermes plugin for automatic call-level model routing.

Routes INTERNAL LLM calls (title gen, memory review, background tasks,
sub-agents) to cheaper model tiers. The user's selected chat model is
never changed.

What gets routed vs what doesn't:
  ✓ api_call_count > 1     → agent is in a tool loop, not the first user turn
  ✓ turn_type == "tool"    → processing tool results
  ✓ turn_type == "title"   → generating a session title
  ✓ turn_type == "memory"  → reviewing memory
  ✓ turn_type == "background" → background review
  ✗ api_call_count == 1 AND turn_type == "user"  → main chat turn, leave alone

Enable:   hermes plugins enable model-router
Disable:  hermes plugins disable model-router
Override: set HERMES_ROUTER_TIER0/1/2/3 env vars
Kill switch: HERMES_MODEL_ROUTER=0

Config (config.yaml):
  plugins:
    entries:
      model-router:
        enabled: true
"""

from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

logger = logging.getLogger(__name__)

# ── Logging ───────────────────────────────────────────────────────────────────

_LOG_DIR  = Path.home() / ".hermes" / "router-logs"
_LOG_FILE = _LOG_DIR / "routing.jsonl"


def _log_decision(entry: dict) -> None:
    try:
        _LOG_DIR.mkdir(parents=True, exist_ok=True)
        with _LOG_FILE.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass  # never block the hook

# Turn types that are ALWAYS internal — never the main user chat turn
_INTERNAL_TURN_TYPES = frozenset({
    "title", "title_generation",
    "memory", "memory_review",
    "background", "background_review",
    "compression", "context_compression",
    "search", "session_search",
    "tool", "tool_result",
    "subagent", "sub_agent",
    "auxiliary", "aux",
    "skill", "skills_hub",
    "mcp",
})

# Maps turn type / call context → call_type key for the router
_TURN_TYPE_TO_CALL_TYPE = {
    "title":              "title",
    "title_generation":   "title",
    "memory":             "summarize",
    "memory_review":      "summarize",
    "background":         "verify",
    "background_review":  "verify",
    "compression":        "summarize",
    "context_compression":"summarize",
    "search":             "analyze",
    "session_search":     "analyze",
    "tool":               "analyze",
    "tool_result":        "analyze",
    "subagent":           "subagent",
    "sub_agent":          "subagent",
    "auxiliary":          "analyze",
    "skill":              "codegen",
    "skills_hub":         "codegen",
    "mcp":                "analyze",
}

# ── Session state (one per session_id) ──────────────────────────────────────

_sessions: dict[str, Any] = {}


def _get_session(session_id: str) -> Any:
    from agent.model_router import RouterSession
    if session_id not in _sessions:
        _sessions[session_id] = RouterSession()
    return _sessions[session_id]


def _clean_old_sessions(keep: int = 32):
    """Keep memory bounded — drop oldest sessions over the cap."""
    if len(_sessions) > keep:
        oldest = list(_sessions.keys())[: len(_sessions) - keep]
        for k in oldest:
            _sessions.pop(k, None)


# ── Plugin hooks ─────────────────────────────────────────────────────────────

def on_session_start(*, session_id: str = "", **_: Any) -> None:
    if session_id:
        _clean_old_sessions()
        from agent.model_router import RouterSession
        _sessions[session_id] = RouterSession()


def on_pre_llm_call(
    *,
    session_id: str = "",
    model: str = "",
    provider: str = "",
    api_call_count: int = 0,
    turn_type: str = "user",
    user_message: Any = None,
    conversation_history: Any = None,
    agent: Any = None,
    **_: Any,
) -> Optional[dict]:
    """Called before every LLM call. Return {"model": <name>} to override."""

    if os.getenv("HERMES_MODEL_ROUTER", "1") == "0":
        return None

    # Never touch the first call of a user turn — that's the main chat turn
    is_main_turn = (api_call_count <= 1) and (turn_type or "user") not in _INTERNAL_TURN_TYPES
    if is_main_turn:
        return None

    # Determine call_type from turn_type
    call_type = _TURN_TYPE_TO_CALL_TYPE.get(turn_type, "analyze")

    # Get session routing state
    session = _get_session(session_id) if session_id else None
    floor = session.complexity_floor if session else 0

    # Boost floor from context tree if available
    try:
        graph = getattr(agent, "_context_graph", None)
        if graph and len(graph) > 0:
            from agent.context_tree import complexity_floor as ctx_floor
            floor = max(floor, ctx_floor(graph, str(user_message or "")))
    except Exception:
        pass

    # Route
    try:
        from agent.model_router import route_call, CallType, ModelTiers
        tiers = ModelTiers()

        # Detect if user is on Claude — use Claude tiers instead
        if _is_claude(model, provider):
            from agent.model_router_claude import ClaudeRouter, ClaudeTiers
            router = ClaudeRouter(ClaudeTiers())
            routed = router.route(
                str(user_message or ""),
                list(conversation_history or []),
                call_type=call_type,
            )
        else:
            # Build a throwaway agent-like object carrying the session floor
            class _FakeAgent:
                pass
            fa = _FakeAgent()
            if session:
                from agent.model_router import RouterSession
                fa._router_session = RouterSession(complexity_floor=floor)
            else:
                fa._router_session = None
            routed = route_call(
                fa,
                CallType(call_type) if call_type in [c.value for c in CallType] else CallType.ANALYZE,
                str(user_message or ""),
                list(conversation_history or []),
                tiers,
            )

        changed = routed and routed != model
        _log_decision({
            "ts":          datetime.now(timezone.utc).isoformat(),
            "source":      "hermes",
            "session_id":  session_id,
            "turn_type":   turn_type,
            "api_call":    api_call_count,
            "call_type":   call_type,
            "model_was":   model or None,
            "model_used":  routed if changed else model,
            "routed":      bool(changed),
            "prompt":      str(user_message or "")[:120],
        })

        if changed:
            logger.debug(
                "model-router plugin: turn_type=%s api_call=%d %s → %s",
                turn_type, api_call_count, model, routed,
            )
            return {"model": routed}

    except Exception as exc:
        logger.debug("model-router plugin: skipped (%s)", exc)

    return None


def on_post_llm_call(
    *,
    session_id: str = "",
    turn_type: str = "user",
    user_message: Any = None,
    conversation_history: Any = None,
    **_: Any,
) -> None:
    """Update session complexity floor after each main turn."""
    if turn_type in _INTERNAL_TURN_TYPES:
        return
    if not session_id:
        return
    session = _get_session(session_id)
    try:
        from agent.model_router import _score_message
        score = _score_message(str(user_message or ""), list(conversation_history or []))
        session.update(score, [], 0)
    except Exception:
        pass


def _is_claude(model: str, provider: str) -> bool:
    m = (model or "").lower()
    p = (provider or "").lower()
    return "claude" in m or p in ("anthropic",)


# ── Plugin registration ───────────────────────────────────────────────────────

def register(ctx) -> None:
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    logger.info("model-router plugin loaded — internal calls will be routed to cheaper tiers")
