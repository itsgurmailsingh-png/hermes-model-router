#!/usr/bin/env python3
"""claude_proxy.py — Reverse proxy for Claude Code main chat model routing.

Intercepts every API call from Claude Code, scores the messages, and
rewrites the "model" field to route to the cheapest capable Claude tier.

Architecture:
    Claude Code → localhost:8082 (this proxy) → api.anthropic.com

Setup:
    export ANTHROPIC_BASE_URL=http://localhost:8082
    python scripts/claude_proxy.py

Then run Claude Code normally. Every call — main chat, sub-agents,
internal — gets routed based on complexity scoring + context tree floor
+ feedback loop. Same logic as the Hermes plugin.

Kill switch:
    export CLAUDE_PROXY_DISABLE=1   (proxy passes through unchanged)
"""

from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

from aiohttp import web, ClientSession

# ── Config ────────────────────────────────────────────────────────────────────

PROXY_PORT = int(os.getenv("CLAUDE_PROXY_PORT", "8082"))
UPSTREAM = os.getenv("ANTHROPIC_UPSTREAM", "https://api.anthropic.com")
ANTHROPIC_VERSION = "2023-06-01"

LOG_DIR = Path.home() / ".claude" / "router-logs"
LOG_FILE = LOG_DIR / "routing.jsonl"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [proxy] %(message)s",
    datefmt="%H:%M:%S",
)
logger = logging.getLogger("claude_proxy")

# ── Scoring (shared with Hermes router) ───────────────────────────────────────

# Try to import from the Hermes router (same scoring logic)
try:
    sys.path.insert(0, str(Path.home() / ".hermes" / "hermes-agent"))
    from agent.model_router_claude import (
        ClaudeRouter, ClaudeTiers, _score, CALL_TYPE_OFFSET
    )
    USE_HERMES_ROUTER = True
    logger.info("Using Hermes ClaudeRouter for scoring")
except Exception as e:
    logger.warning("Hermes router not available (%s), using built-in scoring", e)
    USE_HERMES_ROUTER = False

# ── Built-in scoring fallback (identical to model_router_claude.py) ───────────

_HIGH_SIGNALS = re.compile(
    r"\b(refactor|rewrite|architect|implement|migrate|debug|analy[sz]e|"
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

# Claude tier models
TIER_MODELS = {
    0: os.getenv("CLAUDE_ROUTER_TIER0", "claude-haiku-4-5-20251001"),
    1: os.getenv("CLAUDE_ROUTER_TIER1", "claude-haiku-4-5-20251001"),
    2: os.getenv("CLAUDE_ROUTER_TIER2", "claude-sonnet-4-6"),
    3: os.getenv("CLAUDE_ROUTER_TIER3", "claude-opus-4-6"),
}


def _builtin_score(messages: list[dict]) -> int:
    """Score from the messages array (extract last user message)."""
    # Find the last user message
    user_msg = ""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                # Anthropic format: [{type: "text", text: "..."}, ...]
                user_msg = " ".join(
                    block.get("text", "") for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )
            else:
                user_msg = str(content)
            break

    if _LOW_SIGNALS.match(user_msg):
        return 5

    score = 30
    score += min(20, len(user_msg) // 40)
    hits = len(_HIGH_SIGNALS.findall(user_msg))
    score += min(30, hits * 8)
    if _HAS_CODE.search(user_msg):
        score += 15
    score += min(10, user_msg.count("?") * 4)
    if _TRIVIAL_SCOPE.search(user_msg):
        score -= 15
    if _MICRO_TASKS.search(user_msg):
        score -= 20
    if hits > 0 and len(user_msg) < 60:
        score -= 10

    # History depth
    depth = len(messages)
    score += min(10, depth // 3)

    return max(0, min(100, score))


def _score_to_tier(score: int) -> int:
    if score <= 45:
        return 0  # haiku
    elif score <= 70:
        return 2  # sonnet
    else:
        return 3  # opus


def _tier_to_model(tier: int) -> str:
    return TIER_MODELS.get(tier, TIER_MODELS[0])


# ── Context tree floor (reads Hermes graph) ───────────────────────────────────

GRAPH_FILE = Path.home() / ".hermes" / "router-logs" / "context-graph.json"
FEEDBACK_FILE = Path.home() / ".claude" / "router-logs" / "feedback.json"

# Tag keywords for context floor
_TAG_KEYWORDS = {
    "auth": "auth", "login": "auth", "token": "auth", "jwt": "auth",
    "secur": "security", "encrypt": "security",
    "database": "database", "db": "database", "sql": "database", "schema": "database",
    "api": "api", "endpoint": "api", "route": "api",
    "test": "test", "spec": "test",
    "deploy": "infra", "docker": "infra",
}


def _context_floor(prompt: str) -> int:
    """Read Hermes context graph and compute a semantic floor."""
    try:
        if not GRAPH_FILE.exists():
            return 0
        import json as _json
        graph = _json.loads(GRAPH_FILE.read_text())
        nodes = graph.get("nodes", [])
        if not nodes:
            return 0

        recent = set((graph.get("recent") or [])[-10:])
        lower = prompt.lower()
        tags = set()
        for kw, tag in _TAG_KEYWORDS.items():
            if kw in lower:
                tags.add(tag)

        scored = []
        for n in nodes:
            s = 0.0
            node_tags = set(n.get("tags") or [])
            overlap = node_tags & tags
            s += len(overlap) * 20
            if n.get("id") in recent:
                s += 15
            s += (n.get("complexity") or 0) * 0.1
            if s > 0:
                scored.append((n, s))

        if not scored:
            return 0

        scored.sort(key=lambda x: x[1], reverse=True)
        top4 = scored[:4]
        complexities = [n[0].get("complexity") or 0 for n in top4]
        top = max(complexities)
        avg = sum(complexities) / len(complexities)
        cap = int(os.getenv("HERMES_ROUTER_CTX_FLOOR_CAP", "70"))
        return min(cap, int((top + avg) / 2))
    except Exception:
        return 0


# ── Feedback loop ─────────────────────────────────────────────────────────────

_feedback: dict[str, dict] = {}


def _load_feedback():
    global _feedback
    try:
        if FEEDBACK_FILE.exists():
            _feedback = json.loads(FEEDBACK_FILE.read_text())
    except Exception:
        pass


def _feedback_floor(call_type: str) -> int:
    entry = _feedback.get(call_type)
    if not entry:
        return 0
    return entry.get("floor_boost", 0)


def _save_feedback():
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        FEEDBACK_FILE.write_text(json.dumps(_feedback, indent=2))
    except Exception:
        pass


def _record_result(call_type: str, success: bool):
    ct = call_type or "plan"
    entry = _feedback.setdefault(ct, {"fails": 0, "total": 0, "floor_boost": 0})
    entry["total"] += 1
    if not success:
        entry["fails"] += 1
        entry["floor_boost"] = min(30, entry["floor_boost"] + 10)
    else:
        entry["floor_boost"] = max(0, entry["floor_boost"] - 3)
    _save_feedback()


# ── Logging ────────────────────────────────────────────────────────────────────

def _log_decision(entry: dict):
    try:
        LOG_DIR.mkdir(parents=True, exist_ok=True)
        with LOG_FILE.open("a") as f:
            f.write(json.dumps(entry) + "\n")
    except Exception:
        pass


# ── Call type inference ────────────────────────────────────────────────────────

def _infer_call_type(messages: list[dict], model: str) -> str:
    """Infer call type from the request."""
    # If it's a very short messages array with a system prompt, it might be
    # a title generation or summary
    if len(messages) <= 2:
        # Check for system prompt hints
        for msg in messages:
            content = msg.get("content", "")
            if isinstance(content, str):
                lower = content.lower()
                if "title" in lower or "summarize" in lower or "label" in lower:
                    return "summarize"
                if "review" in lower or "check" in lower:
                    return "verify"
    # Default to plan (main chat turn)
    return "plan"


# ── Manual override detection ──────────────────────────────────────────────────

_TIER_OVERRIDE = re.compile(r"^\s*/t([0-3])\b", re.IGNORECASE)


def _check_override(messages: list[dict]) -> Optional[int]:
    """Check if the last user message has a /t0-t3 override prefix."""
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                for block in content:
                    if isinstance(block, dict) and block.get("type") == "text":
                        m = _TIER_OVERRIDE.match(block.get("text", ""))
                        if m:
                            return int(m.group(1))
            elif isinstance(content, str):
                m = _TIER_OVERRIDE.match(content)
                if m:
                    return int(m.group(1))
            break
    return None


# ── Extract user message text for logging ──────────────────────────────────────

def _extract_user_text(messages: list[dict]) -> str:
    for msg in reversed(messages):
        if msg.get("role") == "user":
            content = msg.get("content", "")
            if isinstance(content, list):
                return " ".join(
                    block.get("text", "") for block in content
                    if isinstance(block, dict) and block.get("type") == "text"
                )[:120]
            return str(content)[:120]
    return ""


# ── Proxy handler ──────────────────────────────────────────────────────────────

async def handle_messages(request: web.Request) -> web.StreamResponse:
    """Handle /v1/messages — score, route, forward."""

    # Read the request body
    body = await request.read()
    try:
        data = json.loads(body)
    except Exception:
        # Can't parse, forward as-is
        return await _forward(request, body, None)

    # Kill switch
    if os.getenv("CLAUDE_PROXY_DISABLE") == "1":
        return await _forward(request, body, data.get("model"))

    messages = data.get("messages", [])
    original_model = data.get("model", "")
    stream = data.get("stream", False)

    # Check for manual override
    override = _check_override(messages)
    if override is not None:
        routed_model = _tier_to_model(override)
        call_type = "override"
        raw_score = -1
        ctx_floor = 0
        feedback = 0
        final_score = -1
    else:
        # Score the request
        call_type = _infer_call_type(messages, original_model)

        if USE_HERMES_ROUTER:
            # Use the Hermes ClaudeRouter
            user_text = _extract_user_text(messages)
            raw_score = _score(user_text, [])
            ctx_floor = _context_floor(user_text)
            feedback = _feedback_floor(call_type)
            offset = CALL_TYPE_OFFSET.get(call_type, 0)
            effective = max(raw_score, ctx_floor, raw_score + feedback)
            final_score = max(0, min(100, effective + offset))
            tier = _score_to_tier(final_score)
        else:
            # Built-in scoring
            user_text = _extract_user_text(messages)
            raw_score = _builtin_score(messages)
            ctx_floor = _context_floor(user_text)
            feedback = _feedback_floor(call_type)
            offset = CALL_TYPE_OFFSET.get(call_type, 0) if USE_HERMES_ROUTER else 0
            effective = max(raw_score, ctx_floor, raw_score + feedback)
            final_score = max(0, min(100, effective + offset))
            tier = _score_to_tier(final_score)

        routed_model = _tier_to_model(tier)

    # Rewrite the model
    data["model"] = routed_model
    new_body = json.dumps(data).encode()

    # Log the decision
    changed = routed_model != original_model
    _log_decision({
        "ts": datetime.now(timezone.utc).isoformat(),
        "source": "proxy",
        "call_type": call_type,
        "raw_score": raw_score,
        "ctx_floor": ctx_floor,
        "feedback": feedback,
        "final_score": final_score,
        "model_was": original_model,
        "model_used": routed_model,
        "routed": changed,
        "prompt": _extract_user_text(messages),
        "stream": stream,
    })

    if changed:
        logger.info(
            "%s score=%d ctx=%d fb=%d final=%d %s → %s  %s",
            call_type, raw_score, ctx_floor, feedback, final_score,
            original_model, routed_model,
            _extract_user_text(messages)[:60],
        )
    else:
        logger.debug(
            "%s score=%d → %s (unchanged)",
            call_type, final_score, routed_model,
        )

    return await _forward(request, new_body, routed_model, stream=stream)


async def _forward(
    request: web.Request,
    body: bytes,
    model: Optional[str],
    stream: bool = False,
) -> web.StreamResponse:
    """Forward the request to the upstream Anthropic API."""

    # Build headers — pass through auth, rewrite content-length
    headers = dict(request.headers)
    headers.pop("Host", None)
    headers.pop("Content-Length", None)
    # Ensure anthropic-version is set
    if "anthropic-version" not in {k.lower() for k in headers}:
        headers["anthropic-version"] = ANTHROPIC_VERSION

    url = f"{UPSTREAM}{request.path_qs}"

    if stream:
        # Stream the response back
        resp = web.StreamResponse(
            status=200,
            headers={
                "Content-Type": "text/event-stream",
                "Cache-Control": "no-cache",
            },
        )
        await resp.prepare(request)

        try:
            async with ClientSession() as session:
                async with session.post(url, data=body, headers=headers) as upstream:
                    async for chunk in upstream.content.iter_any():
                        await resp.write(chunk)
                    await resp.write_eof()
        except Exception as e:
            logger.error("Stream forward error: %s", e)
            await resp.write(f"data: {json.dumps({'type': 'error', 'error': str(e)})}\n\n".encode())
            await resp.write_eof()

        return resp
    else:
        # Non-streaming — forward and return the response
        async with ClientSession() as session:
            async with session.post(url, data=body, headers=headers) as upstream:
                content = await upstream.read()
                return web.Response(
                    status=upstream.status,
                    body=content,
                    content_type=upstream.content_type,
                )


async def handle_health(request: web.Request) -> web.Response:
    """Health check endpoint."""
    return web.json_response({
        "status": "ok",
        "version": "1.2.0",
        "upstream": UPSTREAM,
        "scoring": "hermes" if USE_HERMES_ROUTER else "builtin",
        "feedback_entries": len(_feedback),
    })


async def handle_feedback(request: web.Request) -> web.Response:
    """POST /feedback — record success/failure for the feedback loop.

    Body: {"call_type": "plan", "success": false}
    """
    try:
        data = json.loads(await request.read())
        call_type = data.get("call_type", "plan")
        success = data.get("success", True)
        _record_result(call_type, success)
        return web.json_response({"status": "recorded", "call_type": call_type, "success": success})
    except Exception as e:
        return web.json_response({"error": str(e)}, status=400)


async def handle_stats(request: web.Request) -> web.Response:
    """GET /stats — show routing stats."""
    try:
        entries = []
        if LOG_FILE.exists():
            for line in LOG_FILE.read_text().strip().split("\n"):
                if line.strip():
                    try:
                        entries.append(json.loads(line))
                    except Exception:
                        pass

        proxy_entries = [e for e in entries if e.get("source") == "proxy"]
        total = len(proxy_entries)
        routed = sum(1 for e in proxy_entries if e.get("routed"))

        from collections import Counter
        models = Counter(e.get("model_used", "?") for e in proxy_entries)

        return web.json_response({
            "total": total,
            "routed": routed,
            "models": dict(models.most_common()),
            "feedback": _feedback,
            "scoring": "hermes" if USE_HERMES_ROUTER else "builtin",
        })
    except Exception as e:
        return web.json_response({"error": str(e)}, status=500)


# ── App ───────────────────────────────────────────────────────────────────────

def create_app() -> web.Application:
    app = web.Application()
    app.router.add_post("/v1/messages", handle_messages)
    app.router.add_post("/v1/messages/count_tokens", handle_messages)
    app.router.add_get("/health", handle_health)
    app.router.add_post("/feedback", handle_feedback)
    app.router.add_get("/stats", handle_stats)
    return app


def main():
    _load_feedback()
    logger.info("Claude proxy starting on port %d", PROXY_PORT)
    logger.info("Upstream: %s", UPSTREAM)
    logger.info("Scoring: %s", "hermes" if USE_HERMES_ROUTER else "builtin")
    logger.info("Kill switch: CLAUDE_PROXY_DISABLE=1")
    logger.info("Feedback: POST /feedback {call_type, success}")
    logger.info("Stats: GET /stats")
    logger.info("")
    logger.info("Setup: export ANTHROPIC_BASE_URL=http://localhost:%d", PROXY_PORT)
    web.run_app(create_app(), port=PROXY_PORT, access_log=None)


if __name__ == "__main__":
    main()