#!/usr/bin/env node
// Capture git identity + build time and write build-info.json to two locations:
//   src/lib/build-info.json   — imported by JS, inlined into the bundle by Vite
//   static/build-info.json    — copied to dist/ by adapter-static, read at runtime
//
// Runs as `prepare` (after npm install), `predev`, and `prebuild` so the file
// is always fresh. Failure-tolerant: missing git → placeholder, never throws.

import { execSync } from 'node:child_process';
import { writeFileSync, mkdirSync } from 'node:fs';
import { dirname, join } from 'node:path';
import { fileURLToPath } from 'node:url';

const __dirname = dirname(fileURLToPath(import.meta.url));
const FRONTEND = dirname(__dirname);

function git(args) {
	return execSync(`git ${args}`, {
		cwd: FRONTEND,
		stdio: ['ignore', 'pipe', 'ignore'],
		encoding: 'utf8',
	}).trim();
}

function pad2(n) {
	return n.toString().padStart(2, '0');
}

function buildLabel(date) {
	return [
		date.getFullYear(),
		pad2(date.getMonth() + 1),
		pad2(date.getDate()),
		pad2(date.getHours()) + pad2(date.getMinutes()),
	].join('.');
}

function capture() {
	const now = new Date();
	const buildIso = now.toISOString();
	const label = buildLabel(now);

	let fullHash, shortHash, commitIso, commitSubject, dirty;
	try {
		fullHash = git('rev-parse HEAD');
		shortHash = git('rev-parse --short=7 HEAD');
		commitIso = git('log -1 --format=%cI HEAD');
		commitSubject = git('log -1 --format=%s HEAD');
		dirty = git('status --porcelain').length > 0;
	} catch {
		fullHash = 'unknown';
		shortHash = 'unknown';
		commitIso = buildIso;
		commitSubject = '(git unavailable)';
		dirty = null; // git unavailable: unknown, never clean
	}

	return {
		label,
		short_hash: shortHash,
		full_hash: fullHash,
		commit_iso: commitIso,
		commit_subject: commitSubject,
		dirty,
		build_iso: buildIso,
		app: {
			// The release version, stamped by the app bundle build; a source checkout has none and the host reads git.
			version: process.env.CORSIA_APP_VERSION || null,
		},
	};
}

function writeJson(path, data) {
	mkdirSync(dirname(path), { recursive: true });
	writeFileSync(path, JSON.stringify(data, null, 2) + '\n');
}

const info = capture();
const libPath = join(FRONTEND, 'src', 'lib', 'build-info.json');
const staticPath = join(FRONTEND, 'static', 'build-info.json');

writeJson(libPath, info);
writeJson(staticPath, info);

console.log(
	`build-info: ${info.label} ${info.short_hash}${info.dirty ? '†' : ''}`,
);
