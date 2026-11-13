import { expect } from '@playwright/test';
import { test } from '../support/test';
import { setDebugVersionOverlay, setMockCorsiaAgentModel } from '../support/heldComments';
import { openLoaded, seedLoaded } from '../support/loadedComments';

/**
 * A tap opens the comment it landed on even when the document's text moves between the pointer going up and the frame that reads
 * the tap: the list reloads after feedback is sent and the text can shift under a reader who tapped a moment before. The shift is
 * forced here, once, at the moment the pointer goes up, so the spec fails every time the tap is read from where the text is
 * afterwards instead of from where it was pressed.
 */
const CASES = [
	{ label: '852x393 touch', touch: true, view: { width: 852, height: 393, theme: 'light' as const, touch: true, pane: 'closed' as const } },
	{ label: '1440 pointer', touch: false, view: { width: 1440, height: 900, theme: 'light' as const, touch: false, pane: 'open' as const } },
];

for (const { label, view, touch } of CASES) {
	test.describe(`tap while the text moves ${label}`, () => {
		if (touch) test.use({ hasTouch: true, isMobile: true });
		test(`@flow:doc-comment-tap-while-text-moves a comment opens from a tap whose text moved before it was read (${label})`, async ({ page, request }, info) => {
			test.skip(info.project.name !== 'chromium-desktop', 'every size is set per case on the desktop project');
			await setMockCorsiaAgentModel(request);
			await setDebugVersionOverlay(request, false);
			const doc = await seedLoaded(request, `tap moves ${label}`);
			await openLoaded(page, doc, view);
			const anchor = page.locator('.cm-comment-anchor').filter({ hasText: 'hotel milestone' }).first();
			const scroller = page.locator('.cm-scroller').first();
			await scroller.evaluate((el) => { el.scrollTop = 0; });
			for (let step = 0; step < 40 && !(await anchor.count()); step++) {
				await scroller.evaluate((el) => { el.scrollTop += el.clientHeight * 0.8; });
				await page.waitForTimeout(50);
			}
			await anchor.evaluate((el) => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
			// The pointer goes up and, before the frame that reads the tap, the text moves two rows.
			await page.evaluate(() => window.addEventListener('mouseup', () => { document.querySelector('.cm-scroller')!.scrollTop += 40; }, { capture: true, once: true }));
			if (touch) await anchor.tap(); else await anchor.click();
			const thread = page.locator(`.drawer-panel div.ct, [data-popover] div.ct, [data-comment-id="${doc.longUnreadThread}"] div.ct`).first();
			await expect(thread).toBeVisible();
		});
	});
}
