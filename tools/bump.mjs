#!/usr/bin/env node
/**
 * tools/bump.mjs — bump doer version and regenerate assets.
 */
import { readFileSync, writeFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';
import readline from 'node:readline';

const __dirname  = dirname(fileURLToPath(import.meta.url));
const repoRoot   = resolve(__dirname, '..');
const versionFile = resolve(repoRoot, 'version.json');

const versionData    = JSON.parse(readFileSync(versionFile, 'utf-8'));
const currentVersion = versionData.version || '1.0.0';

function nextPatch(v) {
  const parts = v.split('.').map(Number);
  if (parts.length === 3 && parts.every(n => !Number.isNaN(n)))
    return `${parts[0]}.${parts[1]}.${parts[2] + 1}`;
  return `${v}-next`;
}
const defaultNext = nextPatch(currentVersion);

async function askVersion() {
  const arg = process.argv[2];
  if (arg && !arg.startsWith('-')) return arg;
  if (!process.stdin.isTTY) return defaultNext;
  const rl = readline.createInterface({ input: process.stdin, output: process.stdout });
  return new Promise(res => {
    rl.question(`Current: ${currentVersion}\nNew version [${defaultNext}]: `, ans => {
      rl.close(); res(ans.trim() || defaultNext);
    });
  });
}

const newVersion = await askVersion();
versionData.version = newVersion;
versionData.date = new Date().toISOString().slice(0, 10);
writeFileSync(versionFile, JSON.stringify(versionData, null, 2) + '\n');
console.log(`✓ Updated version.json to ${newVersion}`);

execFileSync('node', [resolve(__dirname, 'generate_logo.mjs')], { stdio: 'inherit' });
execFileSync('git', ['add', 'version.json', 'public/logo.svg', 'public/badge.svg'], { cwd: repoRoot, stdio: 'inherit' });
execFileSync('git', ['commit', '-m', `chore(release): v${newVersion}`], { cwd: repoRoot, stdio: 'inherit' });
console.log(`\n🎉 Bumped and committed v${newVersion}! Run 'git push' to publish.`);
