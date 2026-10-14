import { expect, test, type Page } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import {
	chatComposer, createSession, lastAssistantMessage, lastUserMessage,
	saveEvidenceScreenshot, TEST_TOKEN, watchSessionSubscriptions,
} from '../support/corsia';
import { readMockCallCount, seedMockResponses } from '../support/mock';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

function seedOwnerlessTurn(threadId: string): void {
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !workspace || !stateRoot) {
		throw new Error('Ownerless Stop fixture requires the isolated e2e-local mock environment');
	}
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', `
import sys
from infra.input_writes import begin_batch_write, settle_batch_write
from infra.thread_driver import admit_input
from infra.time import utc_now_iso

thread_id = sys.argv[1]
admission = admit_input(
    thread_id, role="user", content="Previously accepted work", intent="apply_now",
    message_id=thread_id + ":input", turn_id=thread_id + ":orphan",
    boot_epoch="e2e-ownerless", admitted_at=utc_now_iso(),
)
assert admission.batch is not None
begin_batch_write(admission.batch, attempt_id=thread_id + ":write", started_at=utc_now_iso())
settle_batch_write(thread_id + ":write", outcome="accepted")
`, threadId,
	], {
		cwd: repoRoot,
		encoding: 'utf8',
		env: {
			...process.env, PYTHONPATH: path.join(repoRoot, 'backend'),
			CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: stateRoot,
			CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1',
		},
	});
}

async function openThread(page: Page, title: string, threadId: string, login = false): Promise<void> {
	const subscriptions = watchSessionSubscriptions(page);
	await page.goto(login ? `/?token=${encodeURIComponent(TEST_TOKEN)}` : '/');
	await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
	if ((page.viewportSize()?.width ?? 0) <= 768) {
		await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	}
	await page.locator('.thread-item').filter({ hasText: title }).click();
	await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
	await subscriptions.waitForSubscribed(threadId);
}

test('@flow:chat @surface:chat ownerless Stop clears both tabs and permits the next answer', async ({ page, context, request }, testInfo) => {
	const title = `e2e ownerless Stop ${Date.now()}`;
	const threadId = await createSession(request, title);
	await openThread(page, title, threadId, true);
	seedOwnerlessTurn(threadId);
	await openThread(page, title, threadId);
	const observer = await context.newPage();
	try {
		await openThread(observer, title, threadId);
		await page.bringToFront();
		await expect(page.getByRole('button', { name: 'Stop turn' })).toBeVisible();
		await expect(observer.getByRole('button', { name: 'Stop turn' })).toBeVisible();
		await page.getByRole('button', { name: 'Stop turn' }).click();
		await expect(page.locator('.toast.toast--success')).toContainText('Turn stopped');
		for (const tab of [page, observer]) {
			await expect(tab.getByRole('button', { name: 'Stop turn' })).toHaveCount(0);
			await expect(tab.getByRole('button', { name: 'Send message' })).toBeVisible();
			await expect(tab.locator('.toast.toast--error')).toHaveCount(0);
			await expect(tab.locator('.delivery-status')).toHaveCount(0);
		}
		await seedMockResponses(request, [{ content: 'ANSWER_AFTER_OWNERLESS_STOP', finish_reason: 'stop' }]);
		await chatComposer(page).fill('Continue after Stop.');
		await page.getByRole('button', { name: 'Send message' }).click();
		await expect(lastUserMessage(page)).toContainText('Continue after Stop.');
		await expect(lastAssistantMessage(page)).toContainText('ANSWER_AFTER_OWNERLESS_STOP');
		await expect(lastAssistantMessage(observer)).toContainText('ANSWER_AFTER_OWNERLESS_STOP');
		expect(await readMockCallCount(request)).toBe(1);
		await saveEvidenceScreenshot(page, testInfo, 'ownerless-stop');
	} finally {
		await observer.close();
	}
});
