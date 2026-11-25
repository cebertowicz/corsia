import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test, expect } from '../support/test';
import { createSession, openAppPastLanding } from '../support/corsia';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

test('a listed decision option succeeds after typing Something else', async ({ page, request }) => {
	const threadId = await createSession(request, 'Decision answer regression');
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (!python || !workspace || !state || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Requires isolated synthetic state');
	execFileSync(python, [path.join(repoRoot, 'frontend/e2e/support/reply-decision.py'), threadId], {
		cwd: repoRoot,
		env: { ...process.env, PYTHONPATH: 'backend', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state },
	});
	await openAppPastLanding(page);
	await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
	await expect(page.getByRole('heading', { name: 'Choose the launch approach' })).toBeVisible();
	await page.getByRole('radio', { name: 'Something else' }).check();
	await page.getByRole('textbox', { name: 'Your answer' }).fill('A different idea');
	await page.getByRole('radio', { name: /Proceed/ }).check();
	const response = page.waitForResponse(r => r.url().includes('/answer') && r.request().method() === 'POST');
	await page.getByRole('button', { name: 'Answer', exact: true }).click();
	const result = await response;
	expect(result.status()).toBe(200);
	const body = await result.json();
	expect(body.decision).toMatchObject({ state: 'answered', answer: { option_id: 'A', text: null } });
});
