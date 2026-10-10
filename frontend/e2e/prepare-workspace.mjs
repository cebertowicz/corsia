#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const e2eRoot = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(e2eRoot, '..', '..');
const defaultWorkspace = path.join(repoRoot, '.qa', 'e2e-workspace');
const defaultStateRoot = path.join(repoRoot, '.qa', 'e2e-state');
const workspace = process.env.CORSIA_E2E_WORKSPACE ?? defaultWorkspace;
const stateRoot = process.env.CORSIA_E2E_STATE_DIR ?? defaultStateRoot;
const preserve = process.env.CORSIA_E2E_PRESERVE_WORKSPACE === '1';

if (!process.env.CORSIA_E2E_WORKSPACE && !preserve) {
	fs.rmSync(defaultWorkspace, { recursive: true, force: true });
}
if (!process.env.CORSIA_E2E_STATE_DIR && !preserve) {
	fs.rmSync(defaultStateRoot, { recursive: true, force: true });
}

fs.mkdirSync(workspace, { recursive: true });
fs.mkdirSync(stateRoot, { recursive: true });
