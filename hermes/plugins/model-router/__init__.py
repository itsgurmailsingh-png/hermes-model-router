"""model-router — Hermes plugin for automatic call-level model routing.

Routes ALL LLM calls — main chat turns, sub-agents, internal calls
(title gen, memory review, background tasks, tool loops) — to the cheapest
model tier that can handle the task. The user's selected model is the
tier-3 ceiling; simple prompts downgrade to cheaper tiers automatically.

What gets routed:
  ✓ Main user turns (call_type="plan", full score, no offset)
  ✓ api_call_count > 1 (agent in tool loop)
  ✓ turn_type == "tool" (processing tool results)
  ✓ turn_type == "title" (generating session title)
  ✓ turn_type == "memory" (reviewing memory)
  ✓ turn_type == "subagent" (spawned child agents)
  ✓ All other turn types

To skip specific turn types, set HERMES_ROUTER_SKIP_TYPES=title,memory

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

# Context tree state per session: {session_id: (ContextGraph, ContextTreeBuilder)}
_ctx_builders: dict[str, Any] = {}


def _get_session(session_id: str) -> Any:
    try:
        from agent.model_router import RouterSession
    except ImportError:
        from hermes.agent.model_router import RouterSession
    if session_id not in _sessions:
        _sessions[session_id] = RouterSession()
    return _sessions[session_id]


def _get_ctx_builder(session_id: str) -> Optional[Any]:
    """Return the ContextTreeBuilder for this session, or None."""
    entry = _ctx_builders.get(session_id)
    if entry is None:
        return None
    return entry[1]  # (graph, builder) → builder


def _get_ctx_graph(session_id: str) -> Optional[Any]:
    """Return the ContextGraph for this session, or None."""
    entry = _ctx_builders.get(session_id)
    if entry is None:
        return None
    return entry[0]  # (graph, builder) → graph


def _clean_old_sessions(keep: int = 32):
    """Keep memory bounded — drop oldest sessions over the cap."""
    if len(_sessions) > keep:
        oldest = list(_sessions.keys())[: len(_sessions) - keep]
        for k in oldest:
            _sessions.pop(k, None)
            _ctx_builders.pop(k, None)


# ── Plugin hooks ─────────────────────────────────────────────────────────────

def on_session_start(*, session_id: str = "", agent: Any = None, **_: Any) -> None:
    if session_id:
        _clean_old_sessions()
        try:
            from agent.model_router import RouterSession
        except ImportError:
            from hermes.agent.model_router import RouterSession
        _sessions[session_id] = RouterSession()

        # Create context graph + builder for this session
        try:
            try:
                from agent.context_tree.graph import ContextGraph
                from agent.context_tree.builder import ContextTreeBuilder
            except ImportError:
                from hermes.agent.context_tree.graph import ContextGraph
                from hermes.agent.context_tree.builder import ContextTreeBuilder
            graph = ContextGraph(session_id=session_id)
            builder = ContextTreeBuilder(graph, session_id=session_id)
            _ctx_builders[session_id] = (graph, builder)

            # Attach graph to agent so route_call() can use it
            if agent is not None:
                agent._context_graph = graph
                agent._ctx_builder = builder

            logger.debug("model-router: context tree initialised for session %s", session_id)
        except Exception as exc:
            logger.debug("model-router: context tree init failed (%s)", exc)


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

    # Route ALL calls — main chat, sub-agents, internal calls, everything.
    # The user's selected model is treated as the tier-3 ceiling; the router
    # can downgrade to a cheaper tier when the prompt is simple.
    # To disable routing for specific turn types, add them to a
    # HERMES_ROUTER_SKIP_TYPES env var (comma-separated).
    skip_types = os.getenv("HERMES_ROUTER_SKIP_TYPES", "").split(",")
    if (turn_type or "").strip() in skip_types:
        return None

    # Determine call_type from turn_type — main user turns are "plan" (full score)
    call_type = _TURN_TYPE_TO_CALL_TYPE.get(turn_type, "plan")

    # Get session routing state
    session = _get_session(session_id) if session_id else None
    floor = session.complexity_floor if session else 0

    # Boost floor from context tree if available
    try:
        graph = _get_ctx_graph(session_id) if session_id else None
        if graph is None and agent is not None:
            graph = getattr(agent, "_context_graph", None)
        if graph and len(graph) > 0:
            try:
                from agent.context_tree import complexity_floor as ctx_floor
            except ImportError:
                from hermes.agent.context_tree import complexity_floor as ctx_floor
            floor = max(floor, ctx_floor(graph, str(user_message or "")))
    except Exception:
        pass

    # Route
    try:
        try:
            from agent.model_router import route_call, CallType, ModelTiers, RouterSession, load_feedback
        except ImportError:
            from hermes.agent.model_router import route_call, CallType, ModelTiers, RouterSession, load_feedback
        tiers = ModelTiers()

        # Defaults for logging (set in both branches below)
        _raw_score = None
        _ctx_floor_val = 0
        _fb_floor = 0
        _final = None

        # Detect if user is on Claude — use Claude tiers instead
        if _is_claude(model, provider):
            try:
                from agent.model_router_claude import ClaudeRouter, ClaudeTiers
            except ImportError:
                from hermes.agent.model_router_claude import ClaudeRouter, ClaudeTiers
            router = ClaudeRouter(ClaudeTiers())
            # Wire context graph for semantic floor
            graph = _get_ctx_graph(session_id) if session_id else None
            if graph is None and agent is not None:
                graph = getattr(agent, "_context_graph", None)
            if graph is not None:
                router.set_context_graph(graph)
            routed = router.route(
                str(user_message or ""),
                list(conversation_history or []),
                call_type=call_type,
            )
        else:
            # Use the session's RouterSession directly — no FakeAgent needed.
            # We create a minimal agent-like object that carries the session
            # state and context graph, both pulled from the plugin's session dict.
            session_obj = _get_session(session_id) if session_id else RouterSession()
            # Load persisted feedback into the session on first use
            if not session_obj.feedback and session_obj.turns == 0:
                try:
                    session_obj.feedback = load_feedback()
                except Exception:
                    pass

            class _RouterAgent:
                """Minimal agent carrying session + graph for route_call()."""
                def __init__(self, sess, graph):
                    self._router_session = sess
                    self._context_graph = graph

            graph = _get_ctx_graph(session_id) if session_id else None
            if graph is None and agent is not None:
                graph = getattr(agent, "_context_graph", None)
            ra = _RouterAgent(session_obj, graph)

            # Compute score for logging
            try:
                try:
                    from agent.model_router import _score_message
                except ImportError:
                    from hermes.agent.model_router import _score_message
                _raw_score = _score_message(str(user_message or ""), list(conversation_history or []))
            except Exception:
                _raw_score = None

            # Context tree floor for logging
            _ctx_floor_val = 0
            if graph is not None and len(graph) > 0:
                try:
                    try:
                        from agent.context_tree import complexity_floor as _cf
                    except ImportError:
                        from hermes.agent.context_tree import complexity_floor as _cf
                    _ctx_floor_val = _cf(graph, str(user_message or ""))
                except Exception:
                    pass

            # Feedback floor for logging
            _fb_floor = session_obj.feedback_floor(call_type) if session_obj else 0

            routed = route_call(
                ra,
                CallType(call_type) if call_type in [c.value for c in CallType] else CallType.ANALYZE,
                str(user_message or ""),
                list(conversation_history or []),
                tiers,
            )

            # Compute final score for logging
            _final = None
            if _raw_score is not None:
                _effective = max(_raw_score, session_obj.complexity_floor if session_obj else 0)
                _effective = max(_effective, _ctx_floor_val)
                if _fb_floor > 0:
                    _effective = max(_effective, _raw_score + _fb_floor)
                try:
                    from agent.model_router import CALL_TYPE_OFFSET as _CTO
                except ImportError:
                    from hermes.agent.model_router import CALL_TYPE_OFFSET as _CTO
                _offset = _CTO.get(CallType(call_type) if call_type in [c.value for c in CallType] else CallType.ANALYZE, 0)
                _final = max(0, min(100, _effective + _offset))

        changed = routed and routed != model
        _log_decision({
            "ts":          datetime.now(timezone.utc).isoformat(),
            "source":      "hermes",
            "session_id":  session_id,
            "turn_type":   turn_type,
            "api_call":    api_call_count,
            "call_type":   call_type,
            "raw_score":   _raw_score,
            "ctx_floor":   _ctx_floor_val,
            "feedback":    _fb_floor,
            "final_score": _final,
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
        try:
            from agent.model_router import _score_message
        except ImportError:
            from hermes.agent.model_router import _score_message
        score = _score_message(str(user_message or ""), list(conversation_history or []))
        session.update(score, [], 0)
    except Exception:
        pass


def on_tool_result(
    *,
    session_id: str = "",
    tool_name: str = "",
    tool_input: Any = None,
    tool_output: Any = None,
    agent: Any = None,
    **_: Any,
) -> None:
    """Feed tool results to the context tree builder."""
    if not session_id:
        return
    builder = _get_ctx_builder(session_id)
    if builder is None:
        return
    try:
        builder.on_tool_result(
            tool_name=tool_name,
            tool_input=tool_input if isinstance(tool_input, dict) else {},
            tool_output=str(tool_output or ""),
            agent=agent,
        )
    except Exception:
        pass  # never break tool execution


def on_turn_start(
    *,
    session_id: str = "",
    user_message: str = "",
    conversation_history: Any = None,
    agent: Any = None,
    **_: Any,
) -> None:
    """Notify context tree builder of a new turn."""
    if not session_id:
        return
    builder = _get_ctx_builder(session_id)
    if builder is None:
        return
    try:
        builder.on_turn_start(
            user_message=user_message or "",
            history=list(conversation_history or []),
            agent=agent,
        )
    except Exception:
        pass


def on_turn_end(
    *,
    session_id: str = "",
    response: str = "",
    agent: Any = None,
    **_: Any,
) -> None:
    """Save context graph at end of turn."""
    if not session_id:
        return
    builder = _get_ctx_builder(session_id)
    if builder is None:
        return
    try:
        builder.on_turn_end(response=response or "", agent=agent)
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
    ctx.register_hook("on_tool_result", on_tool_result)
    ctx.register_hook("on_turn_start", on_turn_start)
    ctx.register_hook("on_turn_end", on_turn_end)
    logger.info("model-router plugin v1.1 loaded — all calls routed, context tree active")
