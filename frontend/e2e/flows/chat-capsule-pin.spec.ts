import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect, type Page, type TestInfo } from '@playwright/test';
import { createSession, openAppByUrl, sendChatMessage, waitForWsConnected } from '../support/corsia';
import { seedMockResponses } from '../support/mock';

async function openThread(page: Page, title: string): Promise<void> {
	await openAppByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
}

/** Headless Chromium reports no safe area; emulate the phone's top inset through CDP. */
async function setTopInset(page: Page, top: number): Promise<void> {
	const cdp = await page.context().newCDPSession(page);
	await cdp.send('Emulation.setSafeAreaInsetsOverride' as never, { insets: { top, left: 0, bottom: 0, right: 0 } } as never);
}

async function save(page: Page, testInfo: TestInfo, name: string): Promise<void> {
	const root = process.env.CHAT_PIN_PROOF_DIR ?? testInfo.outputPath('chat-pin-proof');
	mkdirSync(root, { recursive: true });
	await page.screenshot({ path: path.join(root, `${name}.png`), animations: 'allow' });
}

for (const { name, width, height, inset, theme } of [
	{ name: '393-inset59-dark', width: 393, height: 852, inset: 59, theme: 'dark' },
	{ name: '393-inset0-light', width: 393, height: 852, inset: 0, theme: 'light' },
	{ name: '1440-light', width: 1440, height: 900, inset: 0, theme: 'light' },
	{ name: '1440-dark', width: 1440, height: 900, inset: 0, theme: 'dark' },
]) {
	test(`@surface:chat-capsule-pin the pinned work capsule sits 8px under the header (${name})`, async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		test.skip(testInfo.project.name !== 'chromium-desktop', 'sizes are set explicitly');
		const title = `chat-motion-${name}-${Date.now()}`;
		await createSession(request, title);
		await page.setViewportSize({ width, height });
		await setTopInset(page, inset);
		await openThread(page, title);
		await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
		await waitForWsConnected(page);
		await seedMockResponses(request, [0, 1, 2].map((index) => ({ content: `Reply ${index} for the motion proof. `.repeat(300), finish_reason: 'stop' })));
		for (let index = 0; index < 3; index++) {
			await sendChatMessage(page, `motion proof message ${index}`);
			await expect(page.locator('.transcript-fold').nth(index)).toBeVisible({ timeout: 20_000 });
		}
		const folds = page.locator('.transcript-fold').filter({ hasText: 'Worked' });
		await expect(folds).toHaveCount(3);
		await expect(page.locator('.transcript-fold.live-work')).toHaveCount(0);
		// The composer keeps focus after a send; blurring it releases the keyboard inset, which moves the lead-in
		// between a click's down and up. Settle that before interacting.
		await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
		await page.waitForTimeout(600);
		for (const index of [0, 1]) {
			await folds.nth(index).locator('> .fold-toggle').click();
			await expect(folds.nth(index).locator('.fold-details')).toBeVisible();
			// Growth below a short list is followed by a scroll to the end a moment later; the next click waits for it.
			await page.waitForTimeout(500);
		}
		await page.waitForTimeout(400);
		await page.locator('.messages').evaluate((el) => {
			const fold = [...el.querySelectorAll<HTMLElement>('.transcript-fold')].filter((node) => node.textContent?.includes('Worked'))[1];
			el.scrollTop = fold.offsetTop + 160;
		});
		await page.waitForTimeout(400);
		const gap = await page.evaluate(() => {
			const header = document.querySelector('.shell-header')!.getBoundingClientRect();
			const capsule = [...document.querySelectorAll('.transcript-fold')].filter((node) => node.textContent?.includes('Worked'))[1].querySelector('.fold-toggle')!.getBoundingClientRect();
			const list = document.querySelector('.messages')!;
			return {
				headerBottom: header.bottom, capsuleTop: capsule.top, gap: capsule.top - header.bottom, scrollTop: list.scrollTop,
				stickyTop: getComputedStyle(list).getPropertyValue('--sticky-top'), listTop: list.getBoundingClientRect().top,
			};
		});
		await save(page, testInfo, `pin-${name}`);
		expect(Math.abs(gap.gap - 8)).toBeLessThanOrEqual(0.5);
	});
}
