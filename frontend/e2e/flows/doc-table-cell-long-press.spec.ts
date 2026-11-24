import { expect } from '@playwright/test';
import { test } from '../support/test';
import { enterDocumentEdit } from '../support/documentReading';
import { openReadingDocument } from '../support/phoneReading';

for (const width of [320, 393]) {
	for (const [mode, slowFrames] of [['Reading', false], ['Edit', false], ['Edit', true]] as const) {
		// slowFrames: every animation frame lands 80 ms late, as on a busy phone, so a selection read scheduled for the next frame
		// always comes after the editor's own focus sync (10 ms). Without it the order is a race and only some runs lose the word.
		test.describe(`@flow:doc-table-cell-long-press @surface:documents ${width} ${mode}${slowFrames ? ' slow frames' : ''}`, () => {
			test.beforeEach(({}, info) => test.skip(info.project.name !== 'chromium-mobile', 'real touch gesture'));
			test('a long-press on a word in a filled table cell keeps the word selected and offers Comment', async ({ page }) => {
				if (slowFrames) {
					await page.addInitScript(() => {
						const raf = window.requestAnimationFrame.bind(window);
						window.requestAnimationFrame = (cb) => window.setTimeout(() => raf(cb), 80);
					});
				}
				await openReadingDocument(page, width);
				if (mode === 'Edit') await enterDocumentEdit(page);
				const cell = page.locator('.cm-md-table td').filter({ hasText: /^Cell passage$/ });
				await cell.scrollIntoViewIfNeeded();
				await page.waitForTimeout(500);
							// A finger held on the word, as the browser's own touch handling sees it (not a scripted range).
				const cdp = await page.context().newCDPSession(page);
				const word = await cell.evaluate((td) => {
					const walker = document.createTreeWalker(td, NodeFilter.SHOW_TEXT);
					const node = walker.nextNode() as Text;
					const range = document.createRange();
					range.setStart(node, 0);
					range.setEnd(node, 4);
					const r = range.getBoundingClientRect();
					return { x: r.x + r.width / 2, y: r.y + r.height / 2, w: r.width, text: node.textContent };
				});
				await cdp.send('Input.synthesizeTapGesture', { x: word.x, y: word.y, duration: 1200, tapCount: 1, gestureSourceType: 'touch' });
				// Past the editor's own focus sync, which is what used to collapse the word.
				await page.waitForTimeout(1200);
				const selected = await page.evaluate(() => document.getSelection()?.toString() ?? '');
				expect(['Cell', 'passage', 'Cell passage']).toContain(selected.trim());
				await expect(page.locator('.sel-comment')).toBeVisible();
			});
		});
	}
}
