import type { Locator, Page } from '@playwright/test';

/** Open the comments drawer from the header pill ("N open threads, …") and return its unsent-messages footer. */
export async function openUnsentFooter(page: Page): Promise<Locator> {
	await page.getByRole('button', { name: /^\d+ open threads?\b/ }).click();
	return page.getByRole('region', { name: 'Document comments' }).locator('[data-drawer-unsent]');
}

/**
 * The feedback Send controls and sending countdowns in the document header that a person could see: text with a box
 * larger than 1 px under no hidden, clipped or collapsed ancestor. Playwright's `toBeVisible` ignores `clip`, so a
 * clipped Send once passed as visible; Send now lives in the comments drawer's footer instead.
 */
export function visibleHeaderSends(page: Page): Promise<string[]> {
	return page.evaluate(() => {
		const header = document.querySelector('.document-header');
		if (!header) return [];
		const hidden = (element: Element) => {
			for (let node: Element | null = element; node; node = node.parentElement) {
				const style = getComputedStyle(node);
				if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return true;
				if (style.position === 'absolute' && style.clip !== 'auto') return true;
				const box = node.getBoundingClientRect();
				if (style.overflow !== 'visible' && (box.width <= 1 || box.height <= 1)) return true;
			}
			return false;
		};
		const found: string[] = [];
		const walker = document.createTreeWalker(header, NodeFilter.SHOW_TEXT);
		for (let text = walker.nextNode(); text; text = walker.nextNode()) {
			if (!/\bSend \d+ comments?\b|\bSend now\b|\bSending to\b/.test(text.textContent ?? '')) continue;
			const range = document.createRange();
			range.selectNodeContents(text);
			const box = range.getBoundingClientRect();
			if (box.width > 1 && box.height > 1 && !hidden(text.parentElement!)) found.push(text.textContent!.trim());
		}
		return found;
	});
}
