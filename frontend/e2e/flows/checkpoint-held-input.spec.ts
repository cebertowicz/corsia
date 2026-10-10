import { expect, test } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chatComposer, createSession, lastAssistantMessage, saveEvidenceScreenshot, TEST_TOKEN, watchSessionSubscriptions } from '../support/corsia';
import { readMockCallCount, seedMockResponses } from '../support/mock';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

function seedHeldInput(threadId: string, intent: string): void {
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !workspace || !stateRoot) {
		throw new Error('Held input fixture requires isolated e2e-local mock state');
	}
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', `
import sys
from core.checkpoint_types import CheckpointUnavailable
from infra.input_writes import begin_batch_write, settle_batch_write
from infra.thread_driver import admit_input, finish_turn
from infra.time import utc_now_iso

thread_id = sys.argv[1]
admission = admit_input(
    thread_id, role="user", content="Keep this exact saved message", intent=sys.argv[2],
    message_id=thread_id + ":held", turn_id=thread_id + ":turn",
    boot_epoch="e2e-held", admitted_at=utc_now_iso(),
)
assert admission.batch is not None
begin_batch_write(admission.batch, attempt_id=thread_id + ":attempt", started_at=utc_now_iso())
settle_batch_write(thread_id + ":attempt", outcome="proved_no_write", error=CheckpointUnavailable(
    "Required checkpoint unavailable", context_window=128000, installed_checkpoint_id=None,
))
terminal = finish_turn(thread_id + ":turn", outcome="error", ended_at=utc_now_iso(),
    successor_turn_id=thread_id + ":unused", boot_epoch="e2e-held")
assert terminal.successor is None
`, threadId, intent,
	], { cwd: repoRoot, encoding: 'utf8', env: {
		...process.env, PYTHONPATH: path.join(repoRoot, 'backend'),
		CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: stateRoot,
		CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1',
	} });
}

for (const intent of ['apply_now', 'queue_after_turn'] as const) {
for (const action of ['recover', 'cancel'] as const) {
	test(`@flow:chat @surface:chat checkpoint-held input ${intent} ${action} preserves the message`, async ({ page, request }, testInfo) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		const title = `e2e held ${action} ${Date.now()}`;
		const threadId = await createSession(request, title);
		const profile = `/api/models/mock-test-model/profile?token=${TEST_TOKEN}`;
		expect((await request.patch(profile, { data: { context_window: 128_000 } })).ok()).toBeTruthy();
		try {
		await seedMockResponses(request, [{ content: 'HELD_INPUT_DELIVERED_ONCE', finish_reason: 'stop' }]);
		seedHeldInput(threadId, intent);
		const subscriptions = watchSessionSubscriptions(page);
		await page.goto(`/?token=${encodeURIComponent(TEST_TOKEN)}`);
		await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await page.locator('.thread-item').filter({ hasText: title }).click();
		await expect(chatComposer(page)).toBeVisible();
		await subscriptions.waitForSubscribed(threadId);
		await expect(page.getByText('Keep this exact saved message', { exact: false }).first()).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'checkpoint-held');
		const waiting = page.locator(intent === 'apply_now' ? '.delivery-status' : '.queued-chip');
		await expect(waiting.filter({ hasText: 'Waiting for checkpoint' }).first()).toBeVisible();
		await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0);
		await expect(page.locator('.delivery-status.sending')).toHaveCount(0);
		const cancel = page.getByRole('button', {
			name: intent === 'apply_now' ? 'Cancel' : 'Remove queued message: Keep this exact saved message', exact: true,
		});
		await expect(cancel).toBeEnabled();
		expect(await readMockCallCount(request)).toBe(0);
			if (action === 'recover') {
				expect((await request.patch(profile, { data: { context_window: 160_000 } })).ok()).toBeTruthy();
				await expect(lastAssistantMessage(page)).toContainText('HELD_INPUT_DELIVERED_ONCE');
				expect(await readMockCallCount(request)).toBe(1);
			} else {
				await cancel.click();
				await expect(page.getByText('Cancelled by you', { exact: true })).toBeVisible();
				expect(await readMockCallCount(request)).toBe(0);
			}
			const response = await request.get(`/api/threads/${threadId}/messages?token=${TEST_TOKEN}`);
			expect(response.ok()).toBeTruthy();
			const rows = (await response.json()).messages.filter((row: { role: string }) => row.role === 'user');
			expect(rows).toHaveLength(1);
			expect(rows[0]).toMatchObject({
				id: `${threadId}:held`, content: 'Keep this exact saved message',
				delivery_state: action === 'recover' ? 'delivered' : 'cancelled',
			});
			await saveEvidenceScreenshot(page, testInfo, `checkpoint-held-${action}`);
		} finally {
			expect((await request.delete(`/api/threads/${threadId}?token=${TEST_TOKEN}`)).ok()).toBeTruthy();
			expect((await request.patch(profile, { data: { context_window: 128_000 } })).ok()).toBeTruthy();
		}
	});
}
}
