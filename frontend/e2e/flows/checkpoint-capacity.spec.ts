import { expect, test, type APIRequestContext } from '@playwright/test';
import {
	chatComposer, createSession, lastAssistantMessage, saveEvidenceScreenshot,
	TEST_TOKEN, watchSessionSubscriptions,
} from '../support/corsia';
import { focusSetting, openOutcomeThread } from '../support/focusOutcomeProof';
import { readMockCallCount, seedMockResponses } from '../support/mock';

async function setContextCap(request: APIRequestContext, contextWindow: number): Promise<void> {
	if (process.env.CORSIA_E2E_MOCK !== '1') {
		throw new Error('Capacity fixture requires the isolated e2e-local mock environment');
	}
	const response = await request.patch(`/api/models/mock-test-model/profile?token=${TEST_TOKEN}`, {
		data: { context_window: contextWindow },
	});
	expect(response.ok()).toBeTruthy();
}

// Focus is a persisted server setting shared by every later spec, so restore it even when a test fails.
test.afterEach(async ({ page }) => {
	await focusSetting(page, true);
});

test('@flow:chat @surface:chat impossible checkpoint cap preserves draft and explains recovery', async ({ page, request }, testInfo) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires the isolated synthetic model catalog');
	const title = `e2e checkpoint capacity ${Date.now()}`;
	const threadId = await createSession(request, title);
	const subscriptions = watchSessionSubscriptions(page);
	await page.goto(`/?token=${encodeURIComponent(TEST_TOKEN)}`);
	await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
	await focusSetting(page, false);
	await openOutcomeThread(page, title);
	await expect(chatComposer(page)).toBeVisible();
	await subscriptions.waitForSubscribed(threadId);
	await seedMockResponses(request, [{ content: 'CAPACITY_RECOVERED', finish_reason: 'stop' }]);
	await setContextCap(request, 16_384);
	try {
		const prompt = 'Keep this unsent draft until the model can accept it.';
		await chatComposer(page).fill(prompt);
		await page.getByRole('button', { name: 'Send message' }).click();
		await expect(chatComposer(page)).toHaveText(prompt);
		await expect(page.getByRole('button', { name: 'Stop turn' })).toHaveCount(0);
		await expect(page.getByRole('button', { name: 'Send message' })).toBeEnabled();
		const messages = await request.get(`/api/threads/${threadId}/messages?token=${TEST_TOKEN}`);
		expect(messages.ok()).toBeTruthy();
		expect((await messages.json()).messages).toHaveLength(0);
		expect(await readMockCallCount(request)).toBe(0);
		await saveEvidenceScreenshot(page, testInfo, 'checkpoint-capacity-rejected');
		await expect(page.getByRole('main')).toContainText(/raise.*context cap|select another model/i);

		await setContextCap(request, 128_000);
		await page.getByRole('button', { name: 'Send message' }).click();
		await expect(lastAssistantMessage(page)).toContainText('CAPACITY_RECOVERED');
		await expect(chatComposer(page)).toHaveText('');
		const delivered = await request.get(`/api/threads/${threadId}/messages?token=${TEST_TOKEN}`);
		expect(delivered.ok()).toBeTruthy();
		const userRows = (await delivered.json()).messages.filter((row: { role: string }) => row.role === 'user');
		expect(userRows).toHaveLength(1);
		expect(userRows[0].content).toBe(prompt);
		expect(userRows[0].delivery_state).toBe('delivered');
		expect(await readMockCallCount(request)).toBe(1);
		await saveEvidenceScreenshot(page, testInfo, 'checkpoint-capacity-recovered');
	} finally {
		await setContextCap(request, 128_000);
		await focusSetting(page, true);
	}
});
