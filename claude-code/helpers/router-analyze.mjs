#!/usr/bin/env node
/**
 * router-analyze.mjs — Analyze model routing decisions.
 *
 * Usage:
 *   node ~/.claude/helpers/router-analyze.mjs           # full report
 *   node ~/.claude/helpers/router-analyze.mjs --last 20 # last N entries
 *   node ~/.claude/helpers/router-analyze.mjs --raw     # dump raw log
 */

import { readFileSync, existsSync } from 'fs';
import { homedir } from 'os';
import { join } from 'path';

const LOG_FILES = [
  join(homedir(), '.claude',  'router-logs', 'routing.jsonl'),  // Claude Code
  join(homedir(), '.hermes',  'router-logs', 'routing.jsonl'),  // Hermes
];
const args = process.argv.slice(2);

const allLines = [];
for (const f of LOG_FILES) {
  if (existsSync(f)) {
    allLines.push(...readFileSync(f, 'utf8').trim().split('\n').filter(Boolean));
  }
}

if (!allLines.length) {
  console.log('No routing log yet. Run some sessions first.');
  process.exit(0);
}

let entries = allLines
  .map(l => { try { return JSON.parse(l); } catch { return null; } })
  .filter(Boolean)
  .sort((a, b) => a.ts < b.ts ? -1 : 1);

// --last N
const lastIdx = args.indexOf('--last');
if (lastIdx !== -1) {
  const n = parseInt(args[lastIdx + 1]) || 20;
  entries = entries.slice(-n);
}

// --raw
if (args.includes('--raw')) {
  entries.forEach(e => console.log(JSON.stringify(e, null, 2)));
  process.exit(0);
}

// ── Summary ──────────────────────────────────────────────────────────────────

const total = entries.length;
const routed = entries.filter(e => e.routed).length;
const byModel = {};
const byCallType = {};
const scores = entries.map(e => e.final_score);

for (const e of entries) {
  byModel[e.model_used] = (byModel[e.model_used] || 0) + 1;
  byCallType[e.call_type] = (byCallType[e.call_type] || 0) + 1;
}

const avg = s => s.length ? Math.round(s.reduce((a, b) => a + b, 0) / s.length) : 0;
const pct = (n, d) => d ? `${Math.round((n / d) * 100)}%` : '0%';

// Estimated cost savings (rough multipliers relative to opus)
const COST = { haiku: 1, sonnet: 15, opus: 75 };
const costWithRouter    = entries.reduce((s, e) => s + (COST[e.model_used] || 75), 0);
const costWithoutRouter = entries.length * COST['opus'];
const saving = Math.round(((costWithoutRouter - costWithRouter) / costWithoutRouter) * 100);

console.log('\n═══════════════════════════════════════════');
console.log('  Model Router — Analysis Report');
console.log('═══════════════════════════════════════════\n');

const fromHermes = entries.filter(e => e.source === 'hermes').length;
const fromClaude = entries.filter(e => e.source !== 'hermes').length;

console.log(`  Total calls logged : ${total}  (hermes: ${fromHermes}  claude-code: ${fromClaude})`);
console.log(`  Calls re-routed    : ${routed} (${pct(routed, total)})`);
console.log(`  Avg complexity     : ${avg(scores)}/100`);
console.log(`  Est. cost saving   : ~${saving}% vs always-opus\n`);

console.log('  Model distribution:');
for (const [model, count] of Object.entries(byModel).sort((a,b) => b[1]-a[1])) {
  const bar = '█'.repeat(Math.round((count / total) * 20));
  console.log(`    ${model.padEnd(10)} ${String(count).padStart(4)}  ${bar}  ${pct(count, total)}`);
}

console.log('\n  Call type breakdown:');
for (const [ct, count] of Object.entries(byCallType).sort((a,b) => b[1]-a[1])) {
  console.log(`    ${ct.padEnd(12)} ${String(count).padStart(4)}  ${pct(count, total)}`);
}

console.log('\n  Score distribution:');
const bands = [[0,20,'trivial'],[21,45,'simple'],[46,70,'medium'],[71,100,'complex']];
for (const [lo, hi, label] of bands) {
  const n = entries.filter(e => e.final_score >= lo && e.final_score <= hi).length;
  const bar = '█'.repeat(Math.round((n / total) * 20));
  console.log(`    ${label.padEnd(10)} [${String(lo).padStart(3)}-${hi}]  ${String(n).padStart(4)}  ${bar}`);
}

console.log('\n  Last 5 decisions:');
for (const e of entries.slice(-5)) {
  const flag = e.routed ? '↺' : '·';
  console.log(`    ${flag} [${e.ts.slice(11,19)}] score=${e.final_score} → ${e.model_used.padEnd(8)} "${e.prompt.slice(0,50)}"`);
}

console.log(`\n  Logs: ${LOG_FILES.filter(existsSync).join(', ')}\n`);
