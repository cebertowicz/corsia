import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
	captureConsoleErrors, createSession, loginByUrl, TEST_TOKEN,
	waitForWsConnected, watchSessionSubscriptions,
} from '../support/corsia';
import { injectMessage } from '../support/mock';

const blocked = 'Browser cleanup could not be verified, so browser work in this conversation is blocked. '
	+ 'To recover, close any leftover agent-browser process or the Chrome window it opened, '
	+ 'then restart CORSIA; the restart clears this block. If the browser can still confirm '
	+ 'closure, asking the agent to close the browser once also clears it. A restart does not '
	+ 'by itself prove that old browser processes are gone.';
const recovered = 'Browser cleanup is now verified. The previously reported browser cleanup blocker is resolved.';

function seedThreadPlan(threadId: string): void {
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !workspace || !stateRoot || !python) {
		throw new Error('Browser cleanup fixture requires isolated e2e-local mock state');
	}
	const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
	// Ensure the canonical thread/workspace hierarchy before the UI reads either plan.
	execFileSync(python, ['-c', `
import sys
from plans.service import ensure_plan
ensure_plan("thread", sys.argv[1])
`, threadId], { cwd: repoRoot, encoding: 'utf8', env: {
		...process.env, PYTHONPATH: path.join(repoRoot, 'backend'),
		CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: stateRoot,
		CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1',
	} });
}

test.describe('@flow:browser-cleanup @surface:chat browser cleanup notices', () => {
	test('warning and certified recovery render live and after reload', async ({ page, request }, testInfo) => {
		const errors = captureConsoleErrors(page);
		const subscriptions = watchSessionSubscriptions(page);
		const title = `e2e browser cleanup ${Date.now()}`;
		const threadId = await createSession(request, title);
		seedThreadPlan(threadId);
		await loginByUrl(page, { waitForComposer: false });
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await page.locator('.thread-item').getByText(title, { exact: true }).click();
		await waitForWsConnected(page);
		await subscriptions.waitForSubscribed(threadId);
		for (const [type, content] of [
			['browser_cleanup_error', blocked], ['browser_cleanup_recovered', recovered],
		]) {
			await injectMessage(request, threadId, { role: 'system', content, metadata: { type } }, TEST_TOKEN);
			await expect(page.getByText(content, { exact: true })).toBeVisible();
		}
		await page.reload();
		// Reload opens Activity; reopen the conversation to read its persisted notices.
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await page.locator('.thread-item').getByText(title, { exact: true }).click();
		await expect(page.getByText(blocked, { exact: true })).toBeVisible();
		await expect(page.getByText(recovered, { exact: true })).toBeVisible();
		await expect(page.locator('.message.system').filter({ hasText: blocked })).toHaveCount(1);
		await expect(page.locator('.message.system').filter({ hasText: recovered })).toHaveCount(1);
		expect(errors).toEqual([]);
		await page.screenshot({ path: testInfo.outputPath('browser-cleanup-notices.png'), fullPage: true, animations: 'disabled' });
	});
});
