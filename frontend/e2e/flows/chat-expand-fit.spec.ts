import { expect, test, type Page } from '@playwright/test';
import { createSession, openAppByUrl, sendChatMessage, waitForWsConnected } from '../support/corsia';
import { seedMockResponses } from '../support/mock';

async function openThread(page: Page, title: string): Promise<void> {
	await openAppByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
}

test('@surface:chat-expand-fit a settled expand stays inside its parent when its content cannot wrap', async ({ page, request }, testInfo) => {
	test.setTimeout(90_000);
	test.skip(testInfo.project.name !== 'chromium-desktop', 'sizes are set explicitly');
	const title = `chat-expand-fit-${Date.now()}`;
	await createSession(request, title);
	await page.setViewportSize({ width: 393, height: 852 });
	await openThread(page, title);
	await waitForWsConnected(page);
	await seedMockResponses(request, [{ content: 'Reply for the expand fit check.', finish_reason: 'stop' }]);
	await sendChatMessage(page, 'expand fit');
	const fold = page.locator('.transcript-fold').filter({ hasText: 'Worked' }).first();
	await expect(fold).toBeVisible({ timeout: 20_000 });
	await expect(page.locator('.transcript-fold.live-work')).toHaveCount(0);
	await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
	await page.waitForTimeout(600);
	await fold.locator('> .fold-toggle').click();
	const region = fold.locator('.expand.settled').first();
	await expect(region).toBeVisible();
	// A row that truncates instead of wrapping, as a tool row or an error output does.
	await region.evaluate((el) => {
		const wide = document.createElement('div');
		wide.setAttribute('data-wide-child', '');
		wide.style.cssText = 'white-space: nowrap; overflow: hidden; text-overflow: ellipsis';
		wide.textContent = 'a row that does not wrap '.repeat(40);
		el.querySelector('.expand-inner')!.appendChild(wide);
	});
	const fit = await region.evaluate((el) => {
		const list = document.querySelector('.messages')!;
		return { regionWidth: el.getBoundingClientRect().width, parentWidth: el.parentElement!.getBoundingClientRect().width, scrollWidth: list.scrollWidth, clientWidth: list.clientWidth, scrollLeft: list.scrollLeft };
	});
	expect(fit.regionWidth).toBeLessThanOrEqual(fit.parentWidth + 0.5);
	expect(fit.scrollWidth).toBeLessThanOrEqual(fit.clientWidth);
	expect(fit.scrollLeft).toBe(0);
});
