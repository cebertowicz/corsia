import { test } from '../support/test';
import { expect } from '@playwright/test';
import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { chatComposer, createSession, lastAssistantMessage, saveEvidenceScreenshot, TEST_TOKEN, watchSessionSubscriptions } from '../support/corsia';
import { focusSetting, openOutcomeThread } from '../support/focusOutcomeProof';
import { setFocusMode } from '../support/focusMode';
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

// Focus is a persisted server setting shared by every later spec, so restore it even when a test fails.
test.afterEach(async ({ request }) => {
	await setFocusMode(request, true);
});

for (const intent of ['apply_now', 'queue_after_turn'] as const) {
	test(`@flow:chat @surface:chat deterministic checkpoint refusal ${intent} is visible and retryable`, async ({ page, request }, testInfo) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		const title = `e2e checkpoint refusal ${intent} ${Date.now()}`;
		const threadId = await createSession(request, title);
		const profile = `/api/models/mock-test-model/profile?token=${encodeURIComponent(TEST_TOKEN)}`;
		expect((await request.patch(profile, { data: { context_window: 128_000 } })).ok()).toBeTruthy();
		try {
			await seedMockResponses(request, [{ content: 'HELD_INPUT_DELIVERED_ONCE', finish_reason: 'stop' }]);
			seedHeldInput(threadId, intent);
			const subscriptions = watchSessionSubscriptions(page);
			await page.goto(`/?token=${encodeURIComponent(TEST_TOKEN)}`);
			await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
			await focusSetting(page, false);
			await openOutcomeThread(page, title);
			await expect(chatComposer(page)).toBeVisible();
			await subscriptions.waitForSubscribed(threadId);
			await expect(page.getByText('Keep this exact saved message', { exact: false }).first()).toBeVisible();
			const refusal = page.getByText('Not delivered: Required checkpoint unavailable', { exact: true });
			await expect(refusal).toBeVisible();
			await expect(page.getByRole('button', { name: 'Retry', exact: true })).toBeEnabled();
			await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0);
			await expect(page.locator('.delivery-status.sending')).toHaveCount(0);
			expect(await readMockCallCount(request)).toBe(0);
			await saveEvidenceScreenshot(page, testInfo, 'checkpoint-refusal-visible');

			// This fixture has no install, cap-change, or model-switch owner. A later
			// profile change cannot silently replay it; the person chooses Retry.
			expect((await request.patch(profile, { data: { context_window: 160_000 } })).ok()).toBeTruthy();
			await expect(refusal).toBeVisible();
			expect(await readMockCallCount(request)).toBe(0);
			await page.getByRole('button', { name: 'Retry', exact: true }).click();
			await expect(lastAssistantMessage(page)).toContainText('HELD_INPUT_DELIVERED_ONCE');
			expect(await readMockCallCount(request)).toBe(1);
			const response = await request.get(`/api/threads/${threadId}/messages?token=${encodeURIComponent(TEST_TOKEN)}`);
			expect(response.ok()).toBeTruthy();
			const rows = (await response.json()).messages.filter((row: { role: string }) => row.role === 'user');
			expect(rows).toHaveLength(2);
			expect(rows.find((row: { id: string }) => row.id === `${threadId}:held`)).toMatchObject({
				content: 'Keep this exact saved message', delivery_state: 'failed', write_outcome: 'proved_no_write',
			});
			expect(rows.find((row: { metadata?: { retried_from_message_id?: string } }) => row.metadata?.retried_from_message_id === `${threadId}:held`)).toMatchObject({
				content: 'Keep this exact saved message', delivery_state: 'delivered', write_outcome: 'accepted',
			});
			await saveEvidenceScreenshot(page, testInfo, 'checkpoint-refusal-retried');
		} finally {
			expect((await request.delete(`/api/threads/${threadId}?token=${encodeURIComponent(TEST_TOKEN)}`)).ok()).toBeTruthy();
			expect((await request.patch(profile, { data: { context_window: 128_000 } })).ok()).toBeTruthy();
		}
	});
}
