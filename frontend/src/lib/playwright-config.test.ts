import { afterEach, describe, expect, it, vi } from 'vitest';

// Every test re-imports playwright.config after vi.resetModules(); the first,
// cold import of @playwright/test alone can pass the 5 s default on a loaded
// CI host.
vi.setConfig({ testTimeout: 30_000 });

const pythonEnv = 'CORSIA_E2E_BACKEND_PYTHON';
// The name scripts/e2e-local documents to users; it exports the internal
// name above, so only a direct Playwright invocation depends on this one.
const documentedPythonEnv = 'CORSIA_E2E_PYTHON';
const mockEnv = 'CORSIA_E2E_MOCK';
const stateEnv = 'CORSIA_E2E_STATE_DIR';
const originalBackendPython = process.env[pythonEnv];
const originalDocumentedPython = process.env[documentedPythonEnv];
const originalMockMode = process.env[mockEnv];
const originalStateRoot = process.env[stateEnv];

afterEach(() => {
	if (originalBackendPython === undefined) delete process.env[pythonEnv];
	else process.env[pythonEnv] = originalBackendPython;
	if (originalDocumentedPython === undefined) delete process.env[documentedPythonEnv];
	else process.env[documentedPythonEnv] = originalDocumentedPython;
	if (originalMockMode === undefined) delete process.env[mockEnv];
	else process.env[mockEnv] = originalMockMode;
	if (originalStateRoot === undefined) delete process.env[stateEnv];
	else process.env[stateEnv] = originalStateRoot;
	vi.resetModules();
});

async function backendCommand(
	backendPython: string | undefined,
	mockMode = false,
	stateRoot?: string,
	documentedPython?: string,
): Promise<string> {
	if (backendPython === undefined) delete process.env[pythonEnv];
	else process.env[pythonEnv] = backendPython;
	if (documentedPython === undefined) delete process.env[documentedPythonEnv];
	else process.env[documentedPythonEnv] = documentedPython;
	if (mockMode) process.env[mockEnv] = '1';
	else delete process.env[mockEnv];
	if (stateRoot === undefined) delete process.env[stateEnv];
	else process.env[stateEnv] = stateRoot;
	vi.resetModules();

	const { default: config } = await import('../../playwright.config');
	if (!Array.isArray(config.webServer)) throw new Error('expected webServer array');
	return config.webServer[0].command;
}

describe('Playwright backend Python command', () => {
	it('shell-quotes the default executable', async () => {
		const command = await backendCommand(undefined);

		expect(command).toContain("'../.venv/bin/python' -m uvicorn");
	});

	it('uses the default executable for an empty override', async () => {
		const command = await backendCommand('');

		expect(command).toContain("'../.venv/bin/python' -m uvicorn");
	});

	it('honours the documented CORSIA_E2E_PYTHON name', async () => {
		// scripts/e2e-local documents this name, but only translates it into
		// CORSIA_E2E_BACKEND_PYTHON when the suite runs through the script.
		// Invoking Playwright directly used to ignore it silently and fall back
		// to an interpreter that exists in no worktree.
		const command = await backendCommand(undefined, false, undefined, '/documented/python');

		expect(command).toContain("'/documented/python' -m uvicorn");
	});

	it('prefers the internal name when both are set, leaving e2e-local unchanged', async () => {
		const command = await backendCommand('/internal/python', false, undefined, '/documented/python');

		expect(command).toContain("'/internal/python' -m uvicorn");
	});

	it('shell-quotes an override containing spaces and metacharacters', async () => {
		const command = await backendCommand("/tmp/python env;review's/python");
		const quotedExecutable = "'/tmp/python env;review'\"'\"'s/python'";

		expect(command).toContain(`${quotedExecutable} -m uvicorn`);
	});

	it('uses the production app target in real mode', async () => {
		expect(await backendCommand(undefined, false)).toContain('-m uvicorn main:app');
	});

	it('uses the isolated E2E app target in mock mode', async () => {
		expect(await backendCommand(undefined, true)).toContain('-m uvicorn e2e_app:app');
	});

	it('marks the isolated backend as test mode', async () => {
		const command = await backendCommand(undefined, false);

		expect(command).toContain('CORSIA_TEST_MODE=1');
	});

	it('pairs the disposable workspace with an explicit disposable state root', async () => {
		const command = await backendCommand(undefined, true);

		expect(command).toContain('CORSIA_STATE_DIR=');
	});

	it('uses the E2E-specific state root selected by the local runner', async () => {
		const command = await backendCommand(undefined, true, '/tmp/corsia seeded state');

		expect(command).toContain("CORSIA_STATE_DIR='/tmp/corsia seeded state'");
	});

	it('lets the preparation script detect and clean its own default roots', async () => {
		const command = await backendCommand(undefined, true);

		expect(command).toMatch(/^node \.\.\/frontend\/e2e\/prepare-workspace\.mjs && /);
	});
});

describe('Playwright frontend dev server', () => {
	it('starts without HMR, so a dev-server reload cannot destroy a running test', async () => {
		vi.resetModules();
		const { default: config } = await import('../../playwright.config');
		if (!Array.isArray(config.webServer)) throw new Error('expected webServer array');

		expect(config.webServer[1].command).toContain('CORSIA_E2E_NO_HMR=1');
	});
});
