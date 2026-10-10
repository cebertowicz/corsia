import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chromium, type FullConfig } from '@playwright/test';
import { openAppByUrl } from './support/corsia';

const e2eRoot = path.dirname(fileURLToPath(import.meta.url));
const frontendRoot = path.resolve(e2eRoot, '..');
const repoRoot = path.resolve(frontendRoot, '..');
const defaultWorkspace = path.join(repoRoot, '.qa', 'e2e-workspace');
const defaultStateRoot = path.join(repoRoot, '.qa', 'e2e-state');

// The dev server answers `/` long before the app can mount: the browser then
// pulls ~700 unbundled modules, each transformed on first request, so a cold
// first load takes seconds on an idle host and outlasts a test's 20 s shell
// wait on a busy one. Load the signed-in shell once here, with a budget sized
// for that cold transform rather than for a test, so every test starts warm.
const COLD_SHELL_BUDGET_MS = 180_000;

async function warmDevServer(baseURL: string | undefined) {
	if (!baseURL) throw new Error('global-setup: playwright.config has no baseURL to warm.');
	const browser = await chromium.launch();
	try {
		// A throwaway context opened through the tests' own helper, so the warm-up
		// signs in and waits for the same shell they do. It only reads (GETs), so
		// it leaves no state for the tests to see.
		const context = await browser.newContext({ baseURL });
		context.setDefaultNavigationTimeout(COLD_SHELL_BUDGET_MS);
		await openAppByUrl(await context.newPage(), { timeout: COLD_SHELL_BUDGET_MS });
	} catch (error) {
		throw new Error(
			`global-setup: the signed-in shell at ${baseURL} did not load and mount within ${COLD_SHELL_BUDGET_MS / 1000}s. ` +
				'The dev server answers but the app never rendered, so no test can start from a ready app.',
			{ cause: error },
		);
	} finally {
		await browser.close();
	}
}

async function globalSetup(config: FullConfig) {
	const workspace = process.env.CORSIA_E2E_WORKSPACE ?? defaultWorkspace;
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR ?? defaultStateRoot;

	fs.mkdirSync(workspace, { recursive: true });
	fs.mkdirSync(stateRoot, { recursive: true });

	await warmDevServer(config.projects[0]?.use.baseURL);
}

export default globalSetup;
