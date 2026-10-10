
import { expect, test } from '@playwright/test';
import {
	createSession,
	loginByUrl,
	saveEvidenceScreenshot,
	waitForWsConnected,
} from '../support/corsia';

test.describe('@flow:sidebar @surface:websocket fresh session live visibility', () => {
	test('API-created chat session appears in the open sidebar without reload', async ({ page, request }, testInfo) => {
		await loginByUrl(page);
		await waitForWsConnected(page);

		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}

		const title = `live-created ${Date.now()}`;
		await createSession(request, title);

		await expect(page.getByText(title).first()).toBeVisible({ timeout: 10_000 });
		await saveEvidenceScreenshot(page, testInfo, 'fresh-session-live-visibility');
	});
});
