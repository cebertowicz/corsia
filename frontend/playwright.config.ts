import { defineConfig, devices } from '@playwright/test';
import path from 'node:path';
import { fileURLToPath } from 'node:url';

const frontendPort = Number(process.env.CORSIA_E2E_FRONTEND_PORT ?? 5178);
const backendPort = Number(process.env.CORSIA_E2E_BACKEND_PORT ?? 3110);
const frontendRoot = path.dirname(fileURLToPath(import.meta.url));
const repoRoot = path.resolve(frontendRoot, '..');
const defaultWorkspace = path.join(repoRoot, '.qa', 'e2e-workspace');
const defaultStateRoot = path.join(repoRoot, '.qa', 'e2e-state');
const workspace = process.env.CORSIA_E2E_WORKSPACE ?? defaultWorkspace;
const stateRoot = process.env.CORSIA_E2E_STATE_DIR ?? defaultStateRoot;
// CORSIA_E2E_PYTHON is the name scripts/e2e-local documents to users;
// CORSIA_E2E_BACKEND_PYTHON is the name that script exports into Playwright.
// Accept both, so invoking Playwright directly honours the documented name
// instead of silently falling back to an interpreter that may not exist.
// The internal name wins when both are set, leaving e2e-local unchanged.
const backendPython =
	process.env.CORSIA_E2E_BACKEND_PYTHON ||
	process.env.CORSIA_E2E_PYTHON ||
	'../.venv/bin/python';
const mockMode = process.env.CORSIA_E2E_MOCK === '1';
const backendApp = mockMode ? 'e2e_app:app' : 'main:app';
// A fault run is deliberately more explicit than ordinary mock E2E: callers
// must attest the roots were freshly prepared before a process may be killed.
const liveFaultMode = mockMode
	&& process.env.CORSIA_E2E_LIVE_FAULTS === '1'
	&& process.env.CORSIA_E2E_FRESH_ROOTS === '1';
const backendUrl = `http://127.0.0.1:${backendPort}`;
const frontendUrl = `http://127.0.0.1:${frontendPort}`;

function shellQuote(value: string): string {
	return `'${value.replace(/'/g, `'"'"'`)}'`;
}

const backendCommand = [
	`node ../frontend/e2e/prepare-workspace.mjs`,
	`&&`,
	mockMode ? `CORSIA_E2E_MOCK=1` : '',
	liveFaultMode
		? `CORSIA_E2E_LIVE_FAULTS=1 CORSIA_E2E_FRESH_ROOTS=1 CORSIA_E2E_BACKEND_PORT=${backendPort} CORSIA_E2E_BACKEND_PYTHON=${shellQuote(backendPython)} CORSIA_TEST_MODE=1 CORSIA_WORKSPACE=${shellQuote(workspace)} CORSIA_STATE_DIR=${shellQuote(stateRoot)} node ../frontend/e2e/live-fault-supervisor.mjs`
		: `CORSIA_TEST_MODE=1 CORSIA_WORKSPACE=${shellQuote(workspace)} CORSIA_STATE_DIR=${shellQuote(stateRoot)} ${shellQuote(backendPython)} -m uvicorn ${backendApp} --host 127.0.0.1 --port ${backendPort}`,
].filter(Boolean).join(' ');

export default defineConfig({
	testDir: './e2e',
	globalSetup: './e2e/global-setup.ts',
	// In mock mode, tests complete in seconds (no real LLM round-trip).
	// In real mode, Haiku one-word replies take 1-3s but slow-network
	// ceiling needs ~60s. Use tighter timeout in mock mode.
	timeout: mockMode ? 30_000 : 90_000,
	fullyParallel: false,
	workers: 1,
	reporter: [
		['list'],
		['html', { outputFolder: 'playwright-report', open: 'never' }],
		['json', { outputFile: 'test-results/e2e.json' }],
	],
	expect: {
		timeout: 10_000,
		toHaveScreenshot: {
			animations: 'disabled',
			maxDiffPixelRatio: 0.01,
		},
	},
	use: {
		baseURL: frontendUrl,
		trace: 'retain-on-failure',
		screenshot: 'only-on-failure',
		video: 'retain-on-failure',
	},
	projects: [
		{
			name: 'chromium-desktop',
			use: {
				...devices['Desktop Chrome'],
				viewport: { width: 1440, height: 1000 },
			},
		},
		{
			name: 'chromium-mobile',
			use: {
				...devices['Pixel 7'],
			},
		},
	],
	webServer: [
		{
			command: backendCommand,
			cwd: path.join(repoRoot, 'backend'),
			url: `${backendUrl}/api/health`,
			timeout: 120_000,
			reuseExistingServer: process.env.CORSIA_E2E_REUSE === '1',
			stdout: 'pipe',
			stderr: 'pipe',
		},
		{
			command: `CORSIA_E2E_NO_HMR=1 CORSIA_BACKEND_URL=${backendUrl} npm run dev -- --host 127.0.0.1 --port ${frontendPort}`,
			cwd: frontendRoot,
			url: frontendUrl,
			timeout: 120_000,
			reuseExistingServer: process.env.CORSIA_E2E_REUSE === '1',
			stdout: 'pipe',
			stderr: 'pipe',
		},
	],
});
