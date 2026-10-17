
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import {
	assistantMessages,
	captureConsoleErrors,
	captureFailedRequests,
	chatComposer,
	createSession,
	lastAssistantMessage,
	lastUserMessage,
	saveEvidenceScreenshot,
	sendChatMessage,
	TEST_SESSION_TOKEN,
	TEST_TOKEN,
	userMessages,
	waitForWsConnected,
} from '../support/corsia';
import { focusSetting, openOutcomeThread } from '../support/focusOutcomeProof';
import { readMockCallCount, releaseMockGate, seedMockResponses, seedPressuredHistory } from '../support/mock';

const CHECKPOINT_REPLY = {
	content: JSON.stringify({
		schema_version: 1,
		objective: 'Continue the exact thread.',
		status: 'in_progress',
		active_constraints: [],
		approvals_and_preferences: [],
		decisions: [],
		rejected_alternatives: [],
		relevant_state: [],
		verification: [],
		known_failures: [],
		open_questions: [],
		blockers: [],
		next_actions: [],
		recently_completed: [],
		facts: [],
		hypotheses: [],
	}),
	finish_reason: 'stop',
};

async function openPressuredSession(
	page: Page,
	request: APIRequestContext,
	titlePrefix: string,
): Promise<{ contextWindow: number; userMessageCount: number }> {
	const sessionTitle = `${titlePrefix} ${Date.now()}`;
	const sessionId = await createSession(request, sessionTitle);
	const pressureFixture = await seedPressuredHistory(request, sessionId, TEST_TOKEN);

	await page.goto('/?token=' + TEST_TOKEN);
	await focusSetting(page, false);
	await openOutcomeThread(page, sessionTitle);
	await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible({ timeout: 20_000 });
	await waitForWsConnected(page);
	await expect(userMessages(page)).toHaveCount(pressureFixture.userMessageCount, { timeout: 10_000 });
	return pressureFixture;
}

test.describe('@flow:chat @surface:chat chat send/receive', () => {
	// Focus is a persisted server setting shared by every later spec, so restore it even when a test fails.
	test.afterEach(async ({ page }) => {
		await focusSetting(page, true);
	});

	test('typed message sends, assistant responds, both render in order', async ({ page, request }, testInfo) => {
		const consoleErrors = captureConsoleErrors(page);
		const failedTurnOrWsRequests = captureFailedRequests(page, (url) =>
			url.includes('/api/turn') || url.includes('/ws'),
		);

		const sessionTitle = `e2e chat-send-receive ${Date.now()}`;
		await createSession(request, sessionTitle);

		// A signed-in person holds a session; lease reads refuse an API credential (backend/infra/auth.py require_human_user).
		await page.goto(`/?token=${encodeURIComponent(TEST_SESSION_TOKEN)}`);
		await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
		// Focus is on by default since #565 and hides finished work; this spec reads the raw transcript.
		await focusSetting(page, false);

		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}

		await page.locator('.thread-item').filter({ hasText: sessionTitle }).click({ timeout: 10_000 });
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible({ timeout: 20_000 });

		await waitForWsConnected(page);

		await seedMockResponses(request, [
			{ content: 'HELLO', finish_reason: 'stop' },
		]);

		const prompt = 'Reply with exactly the word HELLO.';
		await sendChatMessage(page, prompt);

		// playwright.config expect.timeout is ample).
		await expect(lastUserMessage(page)).toContainText(prompt, { timeout: 10_000 });
		expect(await userMessages(page).count()).toBeGreaterThanOrEqual(1);

		await expect(lastAssistantMessage(page)).toBeVisible({ timeout: 15_000 });
		await expect(lastAssistantMessage(page)).not.toBeEmpty({ timeout: 15_000 });
		expect(await assistantMessages(page).count()).toBeGreaterThanOrEqual(1);

		await expect(chatComposer(page)).toHaveText('', { timeout: 5_000 });


		await expect(page.locator('.toast.toast--error')).toHaveCount(0);

		expect(consoleErrors).toEqual([]);

		expect(failedTurnOrWsRequests).toEqual([]);

		await saveEvidenceScreenshot(page, testInfo, 'chat-send-receive');
	});

	test('input-bar Stop ends a held Native turn and leaves the task reusable', async ({ page, request }) => {
		const sessionTitle = `e2e input-bar stop ${Date.now()}`;
		const completionGate = `input-bar-stop-${Date.now()}`;
		const sessionId = await createSession(request, sessionTitle);

		await page.goto(`/?token=${encodeURIComponent(TEST_TOKEN)}`);
		await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible({ timeout: 20_000 });
		await focusSetting(page, false);
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await page.locator('.thread-title', { hasText: sessionTitle }).click({ timeout: 10_000 });
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible({ timeout: 20_000 });
		await waitForWsConnected(page);
		await seedMockResponses(request, [
			{
				content: 'STOPPED_REPLY_MUST_NOT_RENDER',
				completion_gate: completionGate,
				finish_reason: 'stop',
			},
			{ content: 'SUCCESSOR_AFTER_STOP', finish_reason: 'stop' },
		]);

		await sendChatMessage(page, 'Hold this turn until I stop it.');
		await expect.poll(() => readMockCallCount(request)).toBe(1);
		const stopButton = page.getByRole('button', { name: 'Stop turn' });
		await expect(stopButton).toBeVisible({ timeout: 10_000 });
		await stopButton.click();

		await expect(page.locator('.toast.toast--success')).toContainText('Turn stopped');
		await expect(stopButton).toHaveCount(0);
		await expect(page.getByRole('button', { name: 'Send message' })).toBeVisible();
		// Stop ends the Turn, not an input whose write the provider already accepted: that row is left
		// unchanged (backend/tests/test_ownerless_stop.py test_ownerless_postwrite_stop_reaches_exact_terminal);
		// only unfenced input is cancelled and a fenced write with no outcome is ambiguous
		// (same file, test_ownerless_stop_settles_input_from_its_write_evidence). Accepted input has no delivery caption.
		await expect(lastUserMessage(page).locator('.delivery-status')).toHaveCount(0);
		const stoppedMessages = await request.get(`/api/threads/${sessionId}/messages?token=${encodeURIComponent(TEST_TOKEN)}`);
		expect(stoppedMessages.ok()).toBeTruthy();
		const stoppedInput = (await stoppedMessages.json()).messages.find((row: { role: string }) => row.role === 'user');
		expect(stoppedInput).toMatchObject({
			content: 'Hold this turn until I stop it.', delivery_state: 'delivered',
			write_outcome: 'accepted', delivery_error: null,
		});
		await expect(page.getByText('STOPPED_REPLY_MUST_NOT_RENDER')).toHaveCount(0);
		await expect(page.locator('.toast.toast--error')).toHaveCount(0);

		await sendChatMessage(page, 'Reply after the stopped turn.');
		await expect(lastAssistantMessage(page)).toContainText('SUCCESSOR_AFTER_STOP', { timeout: 15_000 });
		await expect(page.locator('.toast.toast--error')).toHaveCount(0);
		expect(await readMockCallCount(request)).toBe(2);
	});

	test('repairable pressured send checkpoints once and delivers the accepted head', async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		const pressureFixture = await openPressuredSession(page, request, 'e2e repairable pressured-send');
		await seedMockResponses(request, [
			CHECKPOINT_REPLY,
			{ content: 'PRESSURED_HEAD_DELIVERED', finish_reason: 'stop' },
		]);

		const prompt = 'new '.repeat(3_000);
		await sendChatMessage(page, prompt);

		await expect(lastUserMessage(page)).toContainText(prompt, { timeout: 20_000 });
		await expect(userMessages(page)).toHaveCount(pressureFixture.userMessageCount + 1, { timeout: 20_000 });
		await expect(lastAssistantMessage(page)).toContainText('PRESSURED_HEAD_DELIVERED', { timeout: 30_000 });
		await expect(page.getByText(/Not delivered:/)).toHaveCount(0);
		await expect(page.getByText(/context (?:checkpoint failed|preflight blocked)/i)).toHaveCount(0);
		await expect(page.locator('.toast.toast--error')).toHaveCount(0);
		expect(await readMockCallCount(request)).toBe(2);
		await saveEvidenceScreenshot(page, testInfo, 'repairable-pressured-send-recovered');
	});

	test('model choice remains actionable while a checkpoint owns the thread', async ({ page, request }) => {
		test.setTimeout(90_000);
		await openPressuredSession(page, request, 'e2e checkpoint model-choice');
		await seedMockResponses(request, [
			{ ...CHECKPOINT_REPLY, completion_gate: 'hold-checkpoint' },
			{ content: 'FIRST_HEAD_DELIVERED', finish_reason: 'stop' },
			{ content: 'FOLLOW_UP_DELIVERED', finish_reason: 'stop' },
		]);

		await sendChatMessage(page, 'new '.repeat(3_000));
		// The checkpoint indicator is the sidebar row's chip (SidebarThreadRow.test.ts: "checkpointing").
		await expect(page.locator('.thread-cutover')).toHaveText('checkpointing', { timeout: 20_000 });

		await page.locator('button.model-chip').click();
		// Choosing the thread's own model is a no-op (InputBarModelSheet.svelte chooseModel), so pick the
		// other seeded model (backend/e2e_mock.py seed_mock_catalog). While a Turn is active the backend does
		// not refuse it: switch_model freezes the FIFO cut and resolves it at the next natural wake
		// (backend/thread/model.py), answering 202 pending, which the sheet marks "Switching…"
		// (account-switch-mid-turn.spec.ts asserts the same marker).
		const modelChoice = page.locator('button.model-list-item', { hasText: 'Mock Test Model' });
		await expect(modelChoice).toBeEnabled();
		await modelChoice.click();
		await expect(modelChoice.locator('.switch-marker')).toHaveText('Switching…');
		await expect(page.locator('.toast.toast--error')).toHaveCount(0);

		await releaseMockGate(request, 'hold-checkpoint');
		await expect(lastAssistantMessage(page)).toContainText(
			'FIRST_HEAD_DELIVERED',
			{ timeout: 30_000 },
		);
		await sendChatMessage(page, 'fresh follow-up');
		await expect(lastAssistantMessage(page)).toContainText(
			'FOLLOW_UP_DELIVERED',
			{ timeout: 20_000 },
		);
	});

	test('irreparable pressured send retains the accepted head without a retry loop', async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		const pressureFixture = await openPressuredSession(
			page,
			request,
			'e2e irreparable pressured-send',
		);
		await seedMockResponses(request, [CHECKPOINT_REPLY]);

		// The shared estimator uses ~3 chars/token. Make the accepted head exceed
		// the target window by itself so no checkpoint tail can make it fit.
		const promptRepeats = Math.floor((pressureFixture.contextWindow * 3) / 'new '.length) + 1;
		const prompt = 'new '.repeat(promptRepeats);
		await sendChatMessage(page, prompt);

		// An unfittable send is a visible failure, not a hold (R16): the exact input stays in the
		// transcript as failed/proved_no_write with its reason (backend/tests/contract/matrix_input_recovery.py,
		// _assert_checkpoint_failure), shown as "Not delivered: <reason>" (frontend/src/lib/messageDelivery.ts).
		const failedInput = lastUserMessage(page);
		const failureCaption = failedInput.locator('.delivery-status');
		await expect(userMessages(page)).toHaveCount(pressureFixture.userMessageCount + 1, { timeout: 30_000 });
		await expect(failedInput).toContainText(prompt, { timeout: 30_000 });
		await expect(failureCaption).toContainText(
			/Not delivered: .*minimum provider continuation exceeds the context window/i,
			{ timeout: 30_000 },
		);
		const callsAfterFailure = await readMockCallCount(request);

		// Nothing retries on its own: the failed row and the provider call count stay put.
		await page.waitForTimeout(1_500);
		await expect(userMessages(page)).toHaveCount(pressureFixture.userMessageCount + 1);
		await expect(failureCaption).toHaveCount(1);
		expect(await readMockCallCount(request)).toBe(callsAfterFailure);
		await saveEvidenceScreenshot(page, testInfo, 'irreparable-pressured-send-retained');
	});
});
