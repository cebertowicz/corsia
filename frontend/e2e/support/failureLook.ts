import { expect, type Locator, type Page, type TestInfo } from '@playwright/test';
import { expectContrast, TEXT } from './contrast';

/**
 * A failed post's reason, shown in its box, reads as an error: in both themes its colour is the error text token
 * (`--red-text`), the one token a reply's failure line, a new comment's box and an image box all use, and it reaches text
 * contrast against what is behind it.
 */
export async function expectFailureLook(page: Page, testInfo: TestInfo, reason: Locator, label: string) {
	for (const theme of ['light', 'dark'] as const) {
		await page.emulateMedia({ colorScheme: theme });
		await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
		const [got, want] = await reason.evaluate((line) => {
			const probe = document.createElement('span');
			probe.style.color = 'var(--red-text)';
			line.parentElement!.append(probe);
			const colours = [getComputedStyle(line).color, getComputedStyle(probe).color];
			probe.remove();
			return colours;
		});
		expect(got, `${label}, ${theme} theme: the reason is in the error colour`).toBe(want);
	}
	await expectContrast(page, testInfo, label, { reason: { target: reason, min: TEXT } });
}
