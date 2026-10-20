import { test } from '../support/test';
import { expect } from '@playwright/test';
import { captureConsoleErrors, createSession, openAppPastLanding, TEST_TOKEN, waitForWsConnected, sidebarThread } from '../support/corsia';
import { setFocusMode } from '../support/focusMode';
import { injectMessage } from '../support/mock';

const WIDE_TABLE = [
	'| Col A | Col B | Col C | Col D | Col E | Col F | Col G | Col H |',
	'| --- | --- | --- | --- | --- | --- | --- | --- |',
	'| aaaaaaaaaa | bbbbbbbbbb | cccccccccc | dddddddddd | eeeeeeeeee | ffffffffff | gggggggggg | hhhhhhhhhh |',
	'| aaaaaaaaaa | bbbbbbbbbb | cccccccccc | dddddddddd | eeeeeeeeee | ffffffffff | gggggggggg | hhhhhhhhhh |',
].join('\n');

test.describe('@flow:agent-table-mobile-overflow @surface:chat agent-sent table stays inside the bubble on mobile', () => {
	test('wide agent-sent table scrolls internally, never clips the viewport or widens the bubble', async ({
		page,
		request,
	}, testInfo) => {
		test.skip((page.viewportSize()?.width ?? 0) > 768, 'mobile-only reproduction');

		// Focus collapses an agent message into a chip; the table only renders in the bubble with Focus off.
		await setFocusMode(request, false);
		const consoleErrors = captureConsoleErrors(page);
		const sessionTitle = `e2e agent-table-mobile-overflow ${Date.now()}`;
		const sessionId = await createSession(request, sessionTitle);

		await injectMessage(
			request,
			sessionId,
			{
				role: 'user',
				content: WIDE_TABLE,
				metadata: {
					sender_kind: 'agent',
					from_thread: 'sender-thread',
					from_title: 'Coordinator',
				},
			},
			TEST_TOKEN,
		);

		await openAppPastLanding(page);
		await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await sidebarThread(page, sessionTitle).click({ timeout: 10_000 });
		await waitForWsConnected(page);

		const agentRow = page
			.locator('.message.user')
			.filter({ has: page.locator('.agent-attr') })
			.first();
		const bubble = agentRow.locator('.user-bubble').first();
		const tableWrap = agentRow.locator('.table-wrap').first();
		await expect(tableWrap).toBeVisible();

		const viewportWidth = page.viewportSize()?.width ?? 0;
		const bubbleBox = await bubble.boundingBox();
		const tableWrapBox = await tableWrap.boundingBox();
		const tableScrollWidth = await tableWrap.evaluate((el) => el.scrollWidth);
		const bodyScrollWidth = await page.evaluate(() => document.documentElement.scrollWidth);

		expect(bubbleBox).not.toBeNull();
		expect(tableWrapBox).not.toBeNull();

		// The bubble must stay within the viewport — this is the exact clip: if the
		// table's intrinsic (min-content) width forces the bubble wider than the
		// viewport, the page gains horizontal scroll and the bubble is cut off.
		expect(bodyScrollWidth).toBeLessThanOrEqual(viewportWidth + 1);
		expect(bubbleBox!.x + bubbleBox!.width).toBeLessThanOrEqual(viewportWidth + 1);

		// The table itself is allowed to overflow its wrapper (that's what makes it
		// scrollable) but the wrapper box itself must not exceed the bubble/viewport.
		expect(tableWrapBox!.width).toBeLessThanOrEqual(viewportWidth + 1);
		expect(tableScrollWidth).toBeGreaterThan(tableWrapBox!.width - 1);

		expect(consoleErrors).toEqual([]);

		await page.screenshot({
			path: testInfo.outputPath('agent-table-mobile-overflow.png'),
			fullPage: true,
			animations: 'disabled',
		});
	});
});
