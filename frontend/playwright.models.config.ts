import { existsSync, lstatSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { defineConfig } from '@playwright/test';
import base from './playwright.config';

const frontendRoot = path.dirname(fileURLToPath(import.meta.url));
const expectedPython = path.resolve(frontendRoot, '../.venv/bin/python');
const configuredPython = process.env.CORSIA_E2E_PYTHON;
if (configuredPython !== expectedPython || !existsSync(expectedPython)) {
	throw new Error(`CORSIA_E2E_PYTHON must name this worktree's checked Python 3.11 environment: ${expectedPython}`);
}

const artifactRoot = process.env.CORSIA_MODELS_E2E_ARTIFACT_DIR;
if (!artifactRoot || !path.isAbsolute(artifactRoot)) {
	throw new Error('CORSIA_MODELS_E2E_ARTIFACT_DIR must be an absolute output root.');
}
if (existsSync(artifactRoot) && lstatSync(artifactRoot).isSymbolicLink()) {
	throw new Error('CORSIA_MODELS_E2E_ARTIFACT_DIR must not be a symlink.');
}

export default defineConfig({
	...base,
	testMatch: /flows\/measurement-models-393\.spec\.ts/,
	outputDir: path.join(artifactRoot, 'artifacts'),
	reporter: [
		['list'],
		['html', { outputFolder: path.join(artifactRoot, 'report'), open: 'never' }],
		['json', { outputFile: path.join(artifactRoot, 'results.json') }],
	],
});
