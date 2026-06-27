#!/usr/bin/env node
/**
 * agent-router.mjs — Hard model routing for Agent tool spawns.
 *
 * Runs as PreToolUse hook on the Agent tool.
 * Reads the tool_input JSON from stdin, scores the prompt,
 * rewrites model field, outputs updated input to stdout.
 *
 * Claude Code applies the rewritten input instead of the original.
 */

import { appendFileSync, mkdirSync, readFileSync, existsSync } from 'fs';
import { homedir } from 'os';
import { join } from 'path';

// ── Context graph reader (reads Hermes graph if available) ────────────────────
const GRAPH_FILE = join(homedir(), '.hermes', 'router-logs', 'context-graph.json');

const TAG_KEYWORDS = {
  auth: 'auth', login: 'auth', token: 'auth', jwt: 'auth',
  secur: 'security', encrypt: 'security',
  database: 'database', db: 'database', sql: 'database', schema: 'database',
  api: 'api', endpoint: 'api', route: 'api',
  test: 'test', spec: 'test',
  deploy: 'infra', docker: 'infra',
};

function promptTags(prompt) {
  const lower = prompt.toLowerCase();
  const tags = new Set();
  for (const [kw, tag] of Object.entries(TAG_KEYWORDS)) {
    if (lower.includes(kw)) tags.add(tag);
  }
  return tags;
}

function contextFloor(prompt) {
  try {
    if (!existsSync(GRAPH_FILE)) return 0;
    const graph = JSON.parse(readFileSync(GRAPH_FILE, 'utf8'));
    const nodes = graph.nodes || [];
    if (!nodes.length) return 0;
    const recent = new Set((graph.recent || []).slice(-10));
    const tags = promptTags(prompt);
    const scored = nodes.map(n => {
      let s = 0;
      const nodeTags = new Set(n.tags || []);
      for (const t of tags) if (nodeTags.has(t)) s += 20;
      if (recent.has(n.id)) s += 15;
      s += (n.complexity || 0) * 0.1;
      return { n, s };
    }).filter(x => x.s > 0).sort((a, b) => b.s - a.s).slice(0, 4);
    if (!scored.length) return 0;
    const top = Math.max(...scored.map(x => x.n.complexity || 0));
    const avg = scored.reduce((s, x) => s + (x.n.complexity || 0), 0) / scored.length;
    return Math.min(70, Math.floor((top + avg) / 2));
  } catch { return 0; }
}

const LOG_DIR  = join(homedir(), '.claude', 'router-logs');
const LOG_FILE = join(LOG_DIR, 'routing.jsonl');

function logDecision(entry) {
  try {
    mkdirSync(LOG_DIR, { recursive: true });
    appendFileSync(LOG_FILE, JSON.stringify(entry) + '\n');
  } catch { /* never block the hook */ }
}

// ── Scoring ──────────────────────────────────────────────────────────────────

const HIGH_SIGNALS = /\b(refactor|rewrite|architect|implement|migrate|debug|analy[sz]e|design|optimize|secur|auth|deploy|integrat|build|create|explain why|why does|how does|compare|review|audit|test suite|end.to.end|pipeline|multi.file|codebase|system|database|schema|api|endpoint|algorithm|entire|complex|production|critical)\b/gi;

const LOW_SIGNALS = /^\s*(hi|hello|hey|thanks|thank you|ok|okay|sure|yes|no|great|done|got it|sounds good|perfect|nice|cool|search|find|look|read|check|list|show)\s*[!.]?\s*$/i;

const HAS_CODE = /```|\bdef \b|\bclass \b|\bfunction\b|\bimport \b/i;

const CALL_TYPE_OFFSET = {
  plan:      0,
  analyze:  -10,
  codegen:  -10,
  verify:   -20,
  summarize:-30,
  title:    -40,
  subagent: -10,
};

function scorePrompt(prompt = '', description = '') {
  const text = `${prompt} ${description}`.trim();

  if (LOW_SIGNALS.test(text)) return 15;

  let score = 30;

  // Length bonus
  score += Math.min(20, Math.floor(text.length / 40));

  // Keyword hits
  const hits = (text.match(HIGH_SIGNALS) || []).length;
  score += Math.min(30, hits * 8);

  // Code block
  if (HAS_CODE.test(text)) score += 15;

  // Multiple files mentioned (by extension OR "N files" / "across files")
  const fileExts = (text.match(/\b\w+\.(py|ts|js|tsx|jsx|go|rs|java|rb|php|dart)\b/gi) || []).length;
  const fileCount = (text.match(/\b([3-9]|\d{2,})\s+files?\b/gi) || []).length;
  const acrossFiles = /\bacross\b.*\bfile|\bmulti.?file|\bseveral files\b/i.test(text);
  if (fileExts > 2 || fileCount > 0 || acrossFiles) score += 15;

  // Question count
  score += Math.min(10, (text.match(/\?/g) || []).length * 4);

  return Math.min(100, score);
}

function inferCallType(prompt = '', subagentType = '') {
  const t = `${prompt} ${subagentType}`.toLowerCase();
  if (/plan|orchestrat|coordinat|architect/.test(t))   return 'plan';
  if (/summar|format|recap|compress|title/.test(t))    return 'summarize';
  if (/verif|review|check|lint|test/.test(t))          return 'verify';
  if (/code|write|implement|generat|creat/.test(t))    return 'codegen';
  if (/analyz|analys|read|search|explore|find/.test(t))return 'analyze';
  return 'plan'; // default to full score
}

function scoreToModel(score) {
  if (score <= 45) return 'haiku';
  if (score <= 70) return 'sonnet';
  return 'opus';
}

// ── Main ─────────────────────────────────────────────────────────────────────

async function main() {
  let raw = '';
  if (!process.stdin.isTTY) {
    for await (const chunk of process.stdin) raw += chunk;
  }

  let hookData = {};
  try { hookData = JSON.parse(raw); } catch { process.exit(0); }

  const toolInput = hookData.tool_input || hookData.toolInput || {};
  const toolName  = hookData.tool_name  || hookData.toolName  || '';

  // Only intercept Agent tool
  if (!/^agent$/i.test(toolName)) {
    process.exit(0);
  }

  // Kill switch
  if (process.env.CLAUDE_MODEL_ROUTER === '0') {
    process.exit(0);
  }

  const prompt      = String(toolInput.prompt      || '');
  const description = String(toolInput.description || '');
  const subtype     = String(toolInput.subagent_type || '');
  const explicitModel = toolInput.model;

  // If model already explicitly set by Claude to something specific, respect it
  // unless it's just the default (no model field = also route)
  // We always route — Claude's suggestion gets overridden here.

  const callType   = inferCallType(prompt, subtype);
  const rawScore   = scorePrompt(prompt, description);
  const offset     = CALL_TYPE_OFFSET[callType] ?? 0;
  const ctxFloor   = contextFloor(prompt);
  const finalScore = Math.max(ctxFloor, Math.min(100, rawScore + offset));
  const model      = scoreToModel(finalScore);

  // Log even when no change needed
  if (explicitModel === model) {
    logDecision({
      ts:          new Date().toISOString(),
      prompt:      prompt.slice(0, 120),
      subtype,
      call_type:   callType,
      raw_score:   rawScore,
      offset,
      final_score: finalScore,
      model_was:   explicitModel || null,
      model_used:  model,
      routed:      false,
    });
    process.exit(0);
  }

  process.stderr.write(
    `[agent-router] call_type=${callType} score=${rawScore}+${offset} ctx_floor=${ctxFloor} final=${finalScore} → ${model}` +
    (explicitModel ? ` (was: ${explicitModel})` : '') + '\n'
  );

  logDecision({
    ts:          new Date().toISOString(),
    prompt:      prompt.slice(0, 120),
    subtype,
    call_type:   callType,
    raw_score:   rawScore,
    offset,
    ctx_floor:   ctxFloor,
    final_score: finalScore,
    model_was:   explicitModel || null,
    model_used:  model,
    routed:      true,
  });

  // Output rewritten tool_input
  const updated = { ...toolInput, model };
  process.stdout.write(JSON.stringify({ tool_input: updated }));
  process.exit(0);
}

main().catch(() => process.exit(0));
