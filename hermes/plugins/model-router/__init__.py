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

# ── Fallback chains ───────────────────────────────────────────────────────────
# When a model returns 503/529/busy, the router tries the next model in the chain.
# Each model maps to its fallback in tier order (cheap → expensive direction for
# busy signals, since busy usually means overloaded at that tier).

_OLLAMA_FALLBACK: dict[str, list[str]] = {
    # T0 busy → try T1 → T2 → T3
    "ministral-3:3b":       ["gemma3:12b", "devstral-small-2:24b", "glm-5.2"],
    # T1 busy → try T0 (different load) or T2
    "gemma3:12b":           ["ministral-3:3b", "devstral-small-2:24b", "glm-5.2"],
    # T2 busy → try T1 → T3
    "devstral-small-2:24b": ["gemma3:12b", "glm-5.2", "ministral-3:3b"],
    # T3 busy → try T2 → T1
    "glm-5.2":              ["devstral-small-2:24b", "gemma3:12b", "ministral-3:3b"],
    # Vision models
    "gemma3:4b":            ["gemma3:12b", "gemma3:27b"],
    "gemma3:27b":           ["gemma3:12b", "devstral-small-2:24b"],
    # Code specialist
    "qwen3-coder:480b":     ["devstral-small-2:24b", "glm-5.2"],
    # Ultra
    "deepseek-v3.1:671b":   ["glm-5.2", "devstral-small-2:24b"],
    "mistral-large-3:675b": ["glm-5.2", "devstral-small-2:24b"],
}

_CLAUDE_FALLBACK: dict[str, list[str]] = {
    "claude-haiku-4-5-20251001": ["claude-sonnet-4-6"],
    "claude-sonnet-4-6":         ["claude-haiku-4-5-20251001", "claude-opus-4-6"],
    "claude-opus-4-6":           ["claude-sonnet-4-6"],
}

# Error status codes / message fragments that mean "model busy, try another"
_BUSY_STATUS_CODES = frozenset({429, 503, 529, 502, 504})
_BUSY_FRAGMENTS = (
    "busy", "overloaded", "unavailable", "rate limit", "rate_limit",
    "too many requests", "model_overloaded", "capacity", "no instances",
    "service unavailable", "gateway timeout", "bad gateway",
)

# Per-session busy model tracking: {session_id: {model: expiry_timestamp}}
_busy_models: dict[str, dict[str, float]] = {}
_BUSY_TTL = 120.0  # seconds to treat a model as busy

# Persisted busy state file — survives across turns and sessions
_BUSY_STATE_FILE = Path.home() / ".hermes" / "router-logs" / "busy-models.json"


def _load_busy_state() -> None:
    """Load persisted busy state from disk on startup."""
    import time
    try:
        if _BUSY_STATE_FILE.exists():
            data = json.loads(_BUSY_STATE_FILE.read_text())
            now = time.monotonic()
            # data format: {model: absolute_expiry_epoch}
            # Convert epoch → monotonic by offset
            epoch_now = __import__("time").time()
            mono_offset = now - epoch_now
            for model, epoch_expiry in data.items():
                mono_expiry = epoch_expiry + mono_offset
                if mono_expiry > now:  # still busy
                    _busy_models.setdefault("_global", {})[model] = mono_expiry
    except Exception:
        pass


def _save_busy_state() -> None:
    """Persist busy state to disk so it survives across turns."""
    import time
    try:
        _BUSY_STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
        now_mono = time.monotonic()
        now_epoch = time.time()
        mono_offset = now_epoch - now_mono
        out = {}
        for session_state in _busy_models.values():
            for model, mono_expiry in session_state.items():
                if mono_expiry > now_mono:
                    epoch_expiry = mono_expiry + mono_offset
                    # Keep the longest remaining expiry per model
                    if model not in out or out[model] < epoch_expiry:
                        out[model] = epoch_expiry
        _BUSY_STATE_FILE.write_text(json.dumps(out))
    except Exception:
        pass


# Load on import
_load_busy_state()

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
        _has_image = False  # default to False

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
            # ── Complexity matrix routing ─────────────────────────────────────
            # Multi-dimensional scoring: text, code, vision, context
            try:
                try:
                    from agent.complexity_matrix import route as matrix_route
                except ImportError:
                    from hermes.agent.complexity_matrix import route as matrix_route
            except Exception:
                # Fallback to old single-score router
                matrix_route = None

            session_obj = _get_session(session_id) if session_id else RouterSession()
            if not session_obj.feedback and session_obj.turns == 0:
                try:
                    session_obj.feedback = load_feedback()
                except Exception:
                    pass

            graph = _get_ctx_graph(session_id) if session_id else None
            if graph is None and agent is not None:
                graph = getattr(agent, "_context_graph", None)

            # Context tree floor
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

            # Feedback floor
            _fb_floor = session_obj.feedback_floor(call_type) if session_obj else 0

            # Call-type offset
            try:
                try:
                    from agent.model_router import CALL_TYPE_OFFSET as _CTO, CallType
                except ImportError:
                    from hermes.agent.model_router import CALL_TYPE_OFFSET as _CTO, CallType
                ct_enum = CallType(call_type) if call_type in [c.value for c in CallType] else CallType.ANALYZE
                _offset = _CTO.get(ct_enum, 0)
            except Exception:
                _offset = 0
                ct_enum = None

            if matrix_route is not None:
                # Use complexity matrix
                routed, dims = matrix_route(
                    text=str(user_message or ""),
                    messages=list(conversation_history or []),
                    history=list(conversation_history or []),
                    user_message=user_message,
                    ctx_tree_floor=_ctx_floor_val,
                    feedback_floor=_fb_floor,
                    call_type_offset=_offset,
                )
                _raw_score = dims.text
                _final = int(dims.combined + _offset)
                _has_image = dims.vision > 0
            else:
                # Fallback: old single-score router
                class _RouterAgent:
                    def __init__(self, sess, graph):
                        self._router_session = sess
                        self._context_graph = graph
                ra = _RouterAgent(session_obj, graph)
                routed = route_call(
                    ra,
                    ct_enum or CallType.ANALYZE,
                    str(user_message or ""),
                    list(conversation_history or []),
                    tiers,
                )
                try:
                    try:
                        from agent.model_router import _score_message
                    except ImportError:
                        from hermes.agent.model_router import _score_message
                    _raw_score = _score_message(str(user_message or ""), list(conversation_history or []))
                except Exception:
                    _raw_score = None
                _final = None
                _has_image = False
                if _raw_score is not None:
                    _effective = max(_raw_score, session_obj.complexity_floor if session_obj else 0)
                    _effective = max(_effective, _ctx_floor_val)
                    if _fb_floor > 0:
                        _effective = max(_effective, _raw_score + _fb_floor)
                    _final = max(0, min(100, _effective + _offset))

        # ── Busy-model check: skip routed model if it's marked busy ──────────
        if routed and session_id and _is_model_busy(session_id, routed):
            fallback = _fallback_for(routed, session_id)
            if fallback:
                logger.warning(
                    "model-router: %s is busy, using fallback %s", routed, fallback
                )
                routed = fallback
            else:
                logger.warning(
                    "model-router: %s is busy and no fallback available, using original %s",
                    routed, model,
                )
                routed = model  # give up and use whatever the user had

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
            "has_image":   _has_image,
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


def on_llm_error(
    *,
    session_id: str = "",
    model: str = "",
    error: Any = None,
    turn_type: str = "user",
    **_: Any,
) -> Optional[dict]:
    """Called when an LLM API call fails.

    If the error is a busy/rate-limit signal, marks the model as busy and
    returns a fallback model for Hermes to retry with immediately.
    Returns {"model": <fallback>} to retry, or None to let Hermes handle it.
    """
    if not error:
        return None
    if not _is_busy_error(error):
        return None  # not a busy error — don't interfere

    current_model = model or ""
    if session_id:
        _mark_busy(session_id, current_model)

    fallback = _fallback_for(current_model, session_id or "")
    if fallback:
        logger.warning(
            "model-router: %s busy (%s) → retrying with %s",
            current_model, type(error).__name__, fallback,
        )
        return {"model": fallback, "retry": True}

    logger.warning(
        "model-router: %s busy and no fallback available — giving up", current_model
    )
    return None


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


def _is_busy_error(exc: Exception) -> bool:
    """Return True if the exception looks like a model-busy / rate-limit error."""
    import time
    status = getattr(exc, "status_code", None) or getattr(
        getattr(exc, "response", None), "status_code", None
    )
    if status in _BUSY_STATUS_CODES:
        return True
    msg = str(exc).lower()
    return any(frag in msg for frag in _BUSY_FRAGMENTS)


def _mark_busy(session_id: str, model: str) -> None:
    """Mark a model as busy globally + per-session for _BUSY_TTL seconds."""
    import time
    expiry = time.monotonic() + _BUSY_TTL
    for key in (session_id, "_global"):
        if key:
            _busy_models.setdefault(key, {})[model] = expiry
    _save_busy_state()
    logger.warning("model-router: marked %s busy for %.0fs", model, _BUSY_TTL)
    _log_decision({
        "ts":         datetime.now(timezone.utc).isoformat(),
        "source":     "hermes",
        "event":      "busy",
        "session_id": session_id,
        "model":      model,
        "ttl":        _BUSY_TTL,
    })


def _is_model_busy(session_id: str, model: str) -> bool:
    """Return True if model is still in the busy window (session or global)."""
    import time
    now = time.monotonic()
    for key in (session_id, "_global"):
        if not key:
            continue
        expiry = _busy_models.get(key, {}).get(model)
        if expiry is None:
            continue
        if now < expiry:
            return True
        # Expired — clean up
        _busy_models.get(key, {}).pop(model, None)
    return False


def _fallback_for(model: str, session_id: str) -> Optional[str]:
    """Return the first non-busy fallback for model, or None if all busy."""
    chain = _OLLAMA_FALLBACK.get(model) or _CLAUDE_FALLBACK.get(model) or []
    # Also check env var overrides (user may have customised tier models)
    for candidate in chain:
        if not _is_model_busy(session_id, candidate):
            return candidate
    return None


def _is_claude(model: str, provider: str) -> bool:
    m = (model or "").lower()
    p = (provider or "").lower()
    return "claude" in m or p in ("anthropic",)


# ── Plugin registration ───────────────────────────────────────────────────────

def register(ctx) -> None:
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("pre_llm_call", on_pre_llm_call)
    ctx.register_hook("post_llm_call", on_post_llm_call)
    ctx.register_hook("on_llm_error", on_llm_error)
    ctx.register_hook("on_tool_result", on_tool_result)
    ctx.register_hook("on_turn_start", on_turn_start)
    ctx.register_hook("on_turn_end", on_turn_end)
    logger.info("model-router plugin v1.2 loaded — routing + fallback + context tree active")
