"""Complexity matrix — multi-dimensional scoring for model routing.

Instead of a single 0-100 score, evaluates 4 dimensions:
  - text:    keyword complexity, length, questions
  - code:    code blocks, imports, functions, multi-file
  - vision:  image attachments present
  - context: session depth, context tree floor, feedback

Each dimension scores 0-100. The router picks the model based on
which dimensions are active and their combined weight.

Model tiers (Ollama Cloud, verified 2026-06-28):
  T0 (tiny):    ministral-3:3b        — greetings, titles, lookups
  T1 (small):   gemma3:12b            — simple Q&A, summaries (vision-capable)
  T2 (medium):  devstral-small-2:24b  — coding, analysis
  T3 (large):   glm-5.2               — planning, architecture, hard reasoning

Vision models (Ollama Cloud):
  V1 (cheap):   gemma3:4b             — simple image description
  V2 (mid):     gemma3:12b            — image analysis with text
  V3 (strong):  gemma3:27b            — complex image reasoning

Code specialist:
  C1 (mid):     devstral-small-2:24b  — general coding
  C2 (strong):  qwen3-coder:480b      — complex codebase, multi-file refactor

Ultra (only for hardest tasks):
  U1:           deepseek-v3.1:671b    — massive codebase, deep reasoning
  U2:           mistral-large-3:675b  — complex analysis
"""

from __future__ import annotations

import re
import os
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


# ── Dimensions ────────────────────────────────────────────────────────────────

@dataclass
class Dimensions:
    """Multi-dimensional complexity scores (each 0-100)."""
    text: int = 0        # keyword complexity, length, questions
    code: int = 0        # code blocks, imports, functions, multi-file
    vision: int = 0      # image attachments (0 = no image, 100 = image present)
    context: int = 0     # session depth, context tree floor, feedback

    @property
    def max_dimension(self) -> str:
        """Name of the highest-scoring dimension."""
        scores = {"text": self.text, "code": self.code, "vision": self.vision, "context": self.context}
        return max(scores, key=scores.get)

    @property
    def combined(self) -> float:
        """Weighted combination — text 40%, code 30%, vision 20%, context 10%."""
        return (
            self.text * 0.4
            + self.code * 0.3
            + self.vision * 0.2
            + self.context * 0.1
        )


# ── Scoring patterns ──────────────────────────────────────────────────────────

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

# Code detection patterns
_HAS_CODE = re.compile(r"```|\bdef \b|\bclass \b|\bfunction\b|\bimport \b", re.IGNORECASE)
_HAS_IMPORTS = re.compile(r"\b(from|import|require)\s+", re.IGNORECASE)
_HAS_CLASSES = re.compile(r"\bclass \w+", re.IGNORECASE)
_HAS_FUNCTIONS = re.compile(r"\bdef \w+|\bfunction \w+|\basync \w+", re.IGNORECASE)
_MULTI_FILE = re.compile(r"\b\w+\.(py|ts|js|tsx|jsx|go|rs|java|rb|php|dart)\b", re.IGNORECASE)


def score_text(text: str) -> int:
    """Text complexity: keywords, length, questions, trivial detection."""
    msg = text or ""
    if _LOW_SIGNALS.match(msg):
        return 5

    score = 30
    score += min(20, len(msg) // 40)

    hits = len(_HIGH_SIGNALS.findall(msg))
    score += min(30, hits * 8)

    score += min(10, msg.count("?") * 4)

    if _TRIVIAL_SCOPE.search(msg):
        score -= 15
    if _MICRO_TASKS.search(msg):
        score -= 20
    if hits > 0 and len(msg) < 60:
        score -= 10

    return max(0, min(100, score))


def score_code(text: str) -> int:
    """Code complexity: code blocks, imports, functions, classes, multi-file."""
    msg = text or ""
    if not msg:
        return 0

    score = 0

    # Fenced code block
    if "```" in msg:
        score += 30

    # Code patterns
    if _HAS_IMPORTS.search(msg):
        score += 15
    if _HAS_CLASSES.search(msg):
        score += 20
    if _HAS_FUNCTIONS.search(msg):
        score += 15

    # Multiple file references
    file_refs = _MULTI_FILE.findall(msg)
    if len(file_refs) >= 2:
        score += 20
    elif len(file_refs) == 1:
        score += 10

    # Code keywords
    code_keywords = re.findall(
        r"\b(refactor|implement|debug|optimize|algorithm|database|schema|"
        r"api|endpoint|pipeline|deploy|migration|test suite)\b",
        msg, re.IGNORECASE,
    )
    score += min(30, len(code_keywords) * 5)

    # Multi-file mention
    if len(file_refs) >= 3:
        score += 15  # 3+ files = complex
    # "entire codebase" / "across N files" / "migrate"
    if re.search(r"\b(entire|whole|all|every|across \d+|\d+ files|migrate|migration)\b", msg, re.IGNORECASE):
        score += 20

    return min(100, score)


def score_vision(messages: List[Dict[str, Any]], user_message: Any = None) -> int:
    """Vision complexity: 0 if no image, 100 if image present."""
    # Check user_message for attachments
    if isinstance(user_message, dict) and user_message.get("attachments"):
        if any(att.get("type") == "image" for att in user_message["attachments"]):
            return 100

    # Check conversation history
    if messages:
        for msg in messages:
            if isinstance(msg, dict):
                content = msg.get("content", "")
                # Check for image content blocks (Anthropic format)
                if isinstance(content, list):
                    for block in content:
                        if isinstance(block, dict) and block.get("type") == "image":
                            return 100
                # Check for attachments
                if msg.get("attachments"):
                    if any(att.get("type") == "image" for att in msg["attachments"]):
                        return 100

    # Check for image-related keywords in text
    if isinstance(user_message, str):
        if re.search(r"\b(attached image|screenshot|photo|picture|image|diagram)\b", user_message, re.IGNORECASE):
            return 50  # text mentions image but no actual attachment

    return 0


def score_context(history: List[Dict[str, Any]], ctx_tree_floor: int = 0, feedback_floor: int = 0) -> int:
    """Context complexity: session depth, context tree, feedback."""
    score = 0

    # History depth
    depth = len(history)
    score += min(30, depth * 3)

    # Tool results in recent history (mid-task)
    tool_msgs = sum(1 for m in history[-6:] if isinstance(m, dict) and m.get("role") == "tool")
    score += min(20, tool_msgs * 5)

    # Context tree floor
    score = max(score, ctx_tree_floor)

    # Feedback floor
    score = max(score, feedback_floor)

    return min(100, score)


# ── Matrix evaluation ─────────────────────────────────────────────────────────

def evaluate(
    text: str,
    messages: List[Dict[str, Any]],
    history: List[Dict[str, Any]],
    user_message: Any = None,
    ctx_tree_floor: int = 0,
    feedback_floor: int = 0,
) -> Dimensions:
    """Evaluate all dimensions and return a Dimensions object."""
    return Dimensions(
        text=score_text(text),
        code=score_code(text),
        vision=score_vision(messages, user_message),
        context=score_context(history, ctx_tree_floor, feedback_floor),
    )


# ── Model selection ────────────────────────────────────────────────────────────

# Model registry — categorized by capability
MODELS = {
    # Tier 0: cheapest, tiny tasks
    "t0": os.getenv("HERMES_ROUTER_TIER0", "ministral-3:3b"),
    # Tier 1: small, simple Q&A (gemma3:12b is vision-capable)
    "t1": os.getenv("HERMES_ROUTER_TIER1", "gemma3:12b"),
    # Tier 2: medium, coding/analysis
    "t2": os.getenv("HERMES_ROUTER_TIER2", "devstral-small-2:24b"),
    # Tier 3: large, planning/architecture
    "t3": os.getenv("HERMES_ROUTER_TIER3", "glm-5.2"),
    # Vision: cheap (gemma3:4b)
    "v1": os.getenv("HERMES_ROUTER_VISION1", "gemma3:4b"),
    # Vision: mid (gemma3:12b — same as t1, vision-capable)
    "v2": os.getenv("HERMES_ROUTER_VISION2", "gemma3:12b"),
    # Vision: strong (gemma3:27b)
    "v3": os.getenv("HERMES_ROUTER_VISION3", "gemma3:27b"),
    # Code specialist: complex codebase
    "c1": os.getenv("HERMES_ROUTER_CODE1", "devstral-small-2:24b"),
    # Code specialist: massive codebase
    "c2": os.getenv("HERMES_ROUTER_CODE2", "qwen3-coder:480b"),
}


def select_model(dims: Dimensions, call_type_offset: int = 0) -> str:
    """Select model based on dimension matrix.

    Routing rules:
    1. Vision: if vision score > 0, route to vision model
       - vision=100 + text high → v3 (gemma3:27b)
       - vision=100 + text low  → v1 (gemma3:4b)
       - vision=50 (text mentions image) → v2 (gemma3:12b)
    2. Code: if code is dominant dimension, route to code specialist
       - code > 60 → c2 (qwen3-coder for massive tasks)
       - code 30-60 → c1 (devstral for general coding)
    3. Text+Context: standard tier routing
       - combined <= 20 → t0 (ministral-3:3b)
       - combined 21-45 → t1 (gemma3:12b)
       - combined 46-70 → t2 (devstral-small-2:24b)
       - combined 71+   → t3 (glm-5.2)
    4. Call-type offset applies to combined score
    """
    # 1. Vision routing
    if dims.vision >= 100:
        if dims.text > 50:
            return MODELS["v3"]  # gemma3:27b — strong vision + complex text
        elif dims.text > 20:
            return MODELS["v2"]  # gemma3:12b — mid vision
        else:
            return MODELS["v1"]  # gemma3:4b — cheap vision
    elif dims.vision >= 50:
        return MODELS["v2"]  # gemma3:12b — text mentions image

    # 2. Code specialist routing
    if dims.code >= 50:
        return MODELS["c2"]  # qwen3-coder:480b — massive codebase
    elif dims.code > 25 and dims.code >= dims.text * 0.6:
        return MODELS["c1"]  # devstral-small-2:24b — general coding

    # 3. Standard tier routing based on combined score
    combined = dims.combined + call_type_offset
    combined = max(0, min(100, combined))

    if combined <= 20:
        return MODELS["t0"]
    elif combined <= 45:
        return MODELS["t1"]
    elif combined <= 70:
        return MODELS["t2"]
    else:
        return MODELS["t3"]


def route(
    text: str,
    messages: List[Dict[str, Any]],
    history: List[Dict[str, Any]],
    user_message: Any = None,
    ctx_tree_floor: int = 0,
    feedback_floor: int = 0,
    call_type_offset: int = 0,
) -> tuple[str, Dimensions]:
    """Full routing: evaluate dimensions → select model.

    Returns (model_name, dimensions) so the caller can log the matrix.
    """
    dims = evaluate(text, messages, history, user_message, ctx_tree_floor, feedback_floor)
    model = select_model(dims, call_type_offset)
    return model, dims