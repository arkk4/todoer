#!/usr/bin/env node
/**
 * tools/generate_logo.mjs
 * Generates public/logo.svg and public/badge.svg from version.json.
 */
import { existsSync, readFileSync } from 'node:fs';
import { resolve, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';
import { execFileSync } from 'node:child_process';

const __dirname   = dirname(fileURLToPath(import.meta.url));
const repoRoot    = resolve(__dirname, '..');
const versionFile = resolve(repoRoot, 'version.json');

// Prefer vendor/logogen submodule; fall back to LOGOGEN_PATH env
const submodulePath = resolve(repoRoot, 'vendor', 'logogen', 'cli', 'logogen.mjs');
const envPath       = process.env.LOGOGEN_PATH;
const logogenPath   = existsSync(submodulePath) ? submodulePath
                    : envPath && existsSync(envPath) ? envPath
                    : null;

if (!logogenPath) {
  console.warn('[generate_logo] logogen not found. Run: git submodule update --init');
  process.exit(0);
}

const config  = JSON.parse(readFileSync(versionFile, 'utf-8'));
const name    = config.name || 'doer';
const version = `v${config.version || '1.0.0'}`;
const pattern = config.logo?.pattern || 'waves';
const palette = Array.isArray(config.logo?.palette)
  ? config.logo.palette.join(',')
  : (config.logo?.palette || '#1e1e2e,#cba6f7,#f38ba8,#fab387,#89b4fa');
const shape  = config.logo?.shape || 'squircle';
const font   = config.logo?.font  || 'sans';

const logoOut  = resolve(repoRoot, 'public', 'logo.svg');
const badgeOut = resolve(repoRoot, 'public', 'badge.svg');

execFileSync('node', [logogenPath, '--name', name, '--version', version,
  '--pattern', pattern, '--palette', palette, '--shape', shape, '--font', font,
  '--out', logoOut], { stdio: 'inherit' });

execFileSync('node', [logogenPath, '--badge', '--name', name, '--version', version,
  '--pattern', pattern, '--palette', palette, '--font', font,
  '--out', badgeOut], { stdio: 'inherit' });

console.log(`✓ Generated public/logo.svg and public/badge.svg (${name} ${version})`);
