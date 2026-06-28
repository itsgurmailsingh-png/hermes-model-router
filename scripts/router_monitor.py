#!/usr/bin/env python3
"""Routing monitor — reads routing decisions and shows a human-readable summary.

Usage:
    python router_monitor.py              # full report
    python router_monitor.py --last 20    # last 20 decisions
    python router_monitor.py --watch      # continuous, refreshes every N seconds

Reads from:
    ~/.hermes/router-logs/routing.jsonl   (Hermes decisions)
    ~/.claude/router-logs/routing.jsonl   (Claude Code decisions)
    ~/.hermes/router-logs/feedback.json   (feedback loop state)
    ~/.hermes/router-logs/context-graph.json (context tree state)
"""
import json
import sys
import time
from pathlib import Path
from datetime import datetime, timezone
from collections import Counter, defaultdict

HERMES_LOG = Path.home() / ".hermes" / "router-logs" / "routing.jsonl"
CLAUDE_LOG = Path.home() / ".claude" / "router-logs" / "routing.jsonl"
FEEDBACK_FILE = Path.home() / ".hermes" / "router-logs" / "feedback.json"
GRAPH_FILE = Path.home() / ".hermes" / "router-logs" / "context-graph.json"

# Colors
R = "\033[0m"
BOLD = "\033[1m"
RED = "\033[31m"
GREEN = "\033[32m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
CYAN = "\033[36m"
DIM = "\033[2m"


def load_log(path: Path) -> list[dict]:
    if not path.exists():
        return []
    entries = []
    for line in path.read_text().strip().split("\n"):
        if not line.strip():
            continue
        try:
            entries.append(json.loads(line))
        except Exception:
            pass
    return entries


def fmt_ts(ts: str) -> str:
    try:
        dt = datetime.fromisoformat(ts.replace("Z", "+00:00"))
        return dt.strftime("%H:%M:%S")
    except Exception:
        return ts[:8] if len(ts) >= 8 else ts


def model_short(model: str) -> str:
    """Short model name for display."""
    if not model:
        return "?"
    m = model.lower()
    if "ministral" in m: return "ministral-3"
    if "gemma3" in m: return "gemma3-12"
    if "devstral" in m: return "devstral-24"
    if "glm" in m: return "glm-5.2"
    if "haiku" in m: return "haiku"
    if "sonnet" in m: return "sonnet"
    if "opus" in m: return "opus"
    return model[:12]


def tier_color(model: str) -> str:
    m = model.lower()
    if any(x in m for x in ["ministral", "haiku"]):
        return GREEN  # cheap
    if any(x in m for x in ["gemma3"]):
        return GREEN
    if any(x in m for x in ["devstral", "sonnet"]):
        return YELLOW  # mid
    if any(x in m for x in ["glm", "opus"]):
        return RED  # expensive
    return CYAN


def print_report(last_n: int | None = None):
    """Print the routing report."""
    hermes = load_log(HERMES_LOG)
    claude = load_log(CLAUDE_LOG)
    all_entries = hermes + claude

    if not all_entries:
        print(f"{DIM}No routing decisions logged yet.{R}")
        return

    # Sort by timestamp
    all_entries.sort(key=lambda e: e.get("ts", ""))

    if last_n:
        entries = all_entries[-last_n:]
    else:
        entries = all_entries

    total = len(entries)
    routed = sum(1 for e in entries if e.get("routed"))
    not_routed = total - routed

    # Model distribution
    models_used = Counter(e.get("model_used", e.get("model_after", "?")) for e in entries)
    models_before = Counter(e.get("model_was", e.get("model_before", "?")) for e in entries)

    # Call type distribution
    call_types = Counter(e.get("call_type", "?") for e in entries)

    # Turn types
    turn_types = Counter(e.get("turn_type", "?") for e in entries if e.get("source") == "hermes")

    print(f"\n{BOLD}═══ Model Router — Routing Report ═══{R}")
    print(f"  {BOLD}Total decisions{R}: {total}")
    print(f"  {BOLD}Routed (changed){R}: {routed} ({routed*100//total if total else 0}%)")
    print(f"  {BOLD}Kept same model{R}: {not_routed}")
    print()

    # Model distribution
    print(f"{BOLD}  Model distribution (what was used):{R}")
    for model, count in models_used.most_common():
        pct = count * 100 // total if total else 0
        bar = "█" * (pct // 3)
        color = tier_color(model)
        print(f"    {color}{model_short(model):<15}{R} {count:>4}  {bar} {pct}%")
    print()

    # Call type distribution
    if call_types:
        print(f"{BOLD}  Call type distribution:{R}")
        for ct, count in call_types.most_common():
            print(f"    {ct:<15} {count:>4}")
        print()

    # Turn types (Hermes only)
    if turn_types:
        print(f"{BOLD}  Turn types (Hermes):{R}")
        for tt, count in turn_types.most_common():
            print(f"    {tt:<15} {count:>4}")
        print()

    # Feedback state
    if FEEDBACK_FILE.exists():
        try:
            fb = json.loads(FEEDBACK_FILE.read_text())
            if fb:
                print(f"{BOLD}  Feedback loop state:{R}")
                for ct, entry in fb.items():
                    boost = entry.get("floor_boost", 0)
                    fails = entry.get("fails", 0)
                    total_ct = entry.get("total", 0)
                    color = RED if boost > 0 else GREEN
                    print(f"    {ct:<15} fails={fails}/{total_ct}  boost={color}+{boost}{R}")
                print()
        except Exception:
            pass

    # Context graph
    if GRAPH_FILE.exists():
        try:
            g = json.loads(GRAPH_FILE.read_text())
            nodes = g.get("nodes", [])
            edges = g.get("edges", [])
            file_nodes = [n for n in nodes if n.get("type") == "FILE"]
            tags = Counter()
            for n in nodes:
                for t in n.get("tags", []):
                    tags[t] += 1
            print(f"{BOLD}  Context graph:{R}")
            print(f"    Nodes: {len(nodes)} ({len(file_nodes)} files)  Edges: {len(edges)}")
            if tags:
                print(f"    Tags: {', '.join(f'{t}({c})' for t, c in tags.most_common(8))}")
            print()
        except Exception:
            pass

    # Recent decisions (last 15)
    print(f"{BOLD}  Recent decisions:{R}")
    recent = entries[-15:] if len(entries) > 15 else entries
    for e in reversed(recent):
        ts = fmt_ts(e.get("ts", ""))
        src = e.get("source", "?")
        ct = e.get("call_type", "?")
        tt = e.get("turn_type", "")
        prompt = e.get("prompt", e.get("prompt_preview", ""))[:50]
        model_was = e.get("model_was", e.get("model_before", ""))
        model_used = e.get("model_used", e.get("model_after", ""))
        routed_flag = e.get("routed", False)
        score = e.get("final_score", e.get("score", "?"))

        if routed_flag:
            arrow = f"{YELLOW}{model_short(model_was)}→{tier_color(model_used)}{model_short(model_used)}{R}"
        else:
            arrow = f"{tier_color(model_used)}{model_short(model_used)}{R}"

        ctx_floor = e.get("ctx_floor", "")
        feedback = e.get("feedback", "")

        extras = []
        if ctx_floor: extras.append(f"ctx={ctx_floor}")
        if feedback: extras.append(f"fb={feedback}")
        extra_str = f" {DIM}[{', '.join(extras)}]{R}" if extras else ""

        print(f"    {DIM}{ts}{R} {src[:1]} {ct:<10} score={str(score):<3} {arrow}  {DIM}{prompt}{R}{extra_str}")

    print()


def watch_mode(interval: int = 10):
    """Continuously refresh the report."""
    try:
        while True:
            # Clear screen
            print("\033[2J\033[H", end="")
            print(f"{DIM}Refreshing every {interval}s — Ctrl+C to exit{R}")
            print_report()
            time.sleep(interval)
    except KeyboardInterrupt:
        print(f"\n{DIM}Stopped.{R}")


if __name__ == "__main__":
    args = sys.argv[1:]
    if "--watch" in args:
        interval = 10
        try:
            idx = args.index("--watch")
            if idx + 1 < len(args):
                interval = int(args[idx + 1])
        except Exception:
            pass
        watch_mode(interval)
    elif "--last" in args:
        try:
            idx = args.index("--last")
            n = int(args[idx + 1])
        except Exception:
            n = 20
        print_report(last_n=n)
    else:
        print_report()