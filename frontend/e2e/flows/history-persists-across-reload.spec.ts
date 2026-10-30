
import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { assistantMessages, chatComposer, e2eModel, loginByUrl, saveEvidenceScreenshot, sendChatMessage, TEST_TOKEN, userMessages, waitForWsConnected, sidebarThread } from '../support/corsia';

const TRIM_FIX_LANDED = process.env.CORSIA_E2E_TRIM_FIX === '1';

const SKIP_PENDING_SPRINT_5_REDESIGN = true;

const SEED_COUNT = Number(process.env.CORSIA_E2E_HISTORY_SEED ?? 350);

test.describe('@flow:history @surface:chat history persists across reload', () => {
	test.skip(!TRIM_FIX_LANDED, 'gated on Slice 5 _trim_to_window fix; set CORSIA_E2E_TRIM_FIX=1 to enable');
	test.skip(SKIP_PENDING_SPRINT_5_REDESIGN, 'Sprint-5 carry-over: concurrent-turn-handling investigation + CORSIA_TEST_MODE=1 seed-endpoint redesign');

	test.setTimeout(15 * 60_000);

	test('reloading a long session re-renders the seeded message history', async ({ page, request }, testInfo) => {
		await loginByUrl(page);

		const sessionTitle = `e2e history-persists ${Date.now()}`;
		const createResp = await request.post(
			`/api/threads?token=${encodeURIComponent(TEST_TOKEN)}`,
			{ data: { title: sessionTitle, model: e2eModel() } },
		);
		expect(createResp.ok()).toBeTruthy();

		await page.goto(`/?token=${encodeURIComponent(TEST_TOKEN)}`);
		await expect(chatComposer(page)).toBeVisible({ timeout: 20_000 });
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await sidebarThread(page, sessionTitle).click({ timeout: 10_000 });

		await waitForWsConnected(page);

		await seedConversation(page, SEED_COUNT);

		const preReloadUserCount = await userMessages(page).count();
		expect(preReloadUserCount).toBeGreaterThanOrEqual(SEED_COUNT);

		await saveEvidenceScreenshot(page, testInfo, 'history-pre-reload');

		await page.reload();
		await expect(chatComposer(page)).toBeVisible({ timeout: 30_000 });
		await waitForWsConnected(page);

		await expect(userMessages(page).first()).toBeVisible({ timeout: 30_000 });

		const postReloadUserCount = await userMessages(page).count();
		expect(postReloadUserCount).toBeGreaterThan(0);

		await expect(userMessages(page).last()).toContainText(`-${SEED_COUNT - 1}`);

		await saveEvidenceScreenshot(page, testInfo, 'history-post-reload');
	});
});

async function seedConversation(page: Page, count: number) {
	for (let i = 0; i < count; i++) {
		const prompt = `seed-msg-${i}: reply with the exact word OK`;
		await sendChatMessage(page, prompt);
		await expect(userMessages(page).nth(i)).toContainText(prompt, { timeout: 30_000 });
		await expect(assistantMessages(page).nth(i)).toBeVisible({ timeout: 60_000 });
	}
}
