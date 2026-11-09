import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { ITEMS, FIRST, card, open } from '../support/marginCards';

/**
 * Text under a comment's highlight is the document's text, so it edits like any other: a click or tap into it, or the arrow keys,
 * put the caret there, and typing, Backspace, Delete and paste change it while the comment stays on it. The card the caret
 * picks still opens (the margin card, the popover below the margin width, the phone sheet) without taking the keystrokes.
 */

const QUOTE = ITEMS[FIRST].quote;
// The paragraph the first comment highlights: the first line holding a highlight, since only comments draw one.
const paragraph = (page: Page) => page.locator('.cm-line:has(.cm-comment-anchor)').first();
const highlight = (page: Page) => paragraph(page).locator('.cm-comment-anchor').first();
/** The highlighted text, read across every span the editor drew it in. */
const highlighted = (page: Page) => paragraph(page).evaluate((line) => [...line.querySelectorAll('.cm-comment-anchor')].map((span) => span.textContent).join(''));

/** Whether the document's editor holds the keyboard focus, and where focus is when it does not. */
const focusIn = (page: Page) => page.evaluate(() => {
	const active = document.activeElement;
	return active?.closest('.cm-content') ? 'editor' : `${active?.tagName.toLowerCase()}.${active?.className}`;
});

/** A paste the way the browser delivers one: to the focused element, carrying the clipboard's text. */
const paste = (page: Page, text: string) => page.evaluate((text) => {
	const data = new DataTransfer();
	data.setData('text/plain', text);
	document.activeElement?.dispatchEvent(new ClipboardEvent('paste', { clipboardData: data, bubbles: true, cancelable: true }));
}, text);

const sheet = (page: Page) => page.locator('[data-adaptive-drawer] .drawer-panel');

/**
 * Puts the caret in the highlight as the person does and checks the card opened (`shown`), then edits there. On a phone
 * (`phone`) the tap opens the sheet, which covers the text; closing it and tapping the text again is editing, and opens nothing.
 */
async function editInsideHighlight(page: Page, shown: () => Promise<void>, phone = false) {
	await expect(highlight(page)).toHaveText(QUOTE);
	if (phone) await highlight(page).tap(); else await highlight(page).click();
	await shown();
	if (phone) {
		await sheet(page).getByRole('button', { name: 'Close', exact: true }).click();
		await expect(sheet(page)).toHaveCount(0);
		await highlight(page).tap();
	}
	// The card opens a beat after the caret lands: what is typed after it must still reach the text.
	await page.waitForTimeout(400);
	if (phone) await expect(sheet(page), 'a tap in the text the caret is already in edits it').toHaveCount(0);
	await expect.poll(() => focusIn(page), { message: 'the editor keeps the focus' }).toBe('editor');
	await page.keyboard.type('QWE');
	await expect.poll(() => highlighted(page), { message: 'typed text lands inside the highlight, which stays on it' }).toContain('QWE');
	await page.keyboard.press('ArrowLeft');
	await page.keyboard.press('ArrowLeft');
	await page.keyboard.press('Backspace');
	await page.keyboard.press('Delete');
	if (!phone) await shown();
	// The caret sat between Q and W: Backspace takes the Q, Delete the W.
	await expect.poll(async () => (await highlighted(page)).replace('E', ''), { message: 'Backspace and Delete remove the characters around the caret' }).toBe(QUOTE);
	expect(await focusIn(page)).toBe('editor');
	await paste(page, 'RTY');
	await expect.poll(async () => (await highlighted(page)).replace('RTYE', ''), { message: 'a paste lands at the caret, inside the highlight' }).toBe(QUOTE);
	// The arrow keys carry the caret into the highlight from the text after it, and typing works there too.
	const after = await paragraph(page).evaluate((line) => {
		const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT);
		for (let node = walker.nextNode(); node; node = walker.nextNode()) {
			const at = node.textContent?.indexOf(' says ') ?? -1;
			if (at < 0) continue;
			const range = document.createRange(); range.setStart(node, at + 2); range.setEnd(node, at + 4);
			const box = range.getBoundingClientRect();
			return { x: box.left + box.width / 2, y: box.top + box.height / 2 };
		}
		return null;
	});
	expect(after, 'the text after the highlight is drawn').not.toBeNull();
	if (phone) await page.touchscreen.tap(after!.x, after!.y); else await page.mouse.click(after!.x, after!.y);
	await expect.poll(() => focusIn(page)).toBe('editor');
	// The first Home reaches the start of the row, the second the start of the line, where the highlight begins.
	await page.keyboard.press('Home');
	await page.keyboard.press('Home');
	await page.keyboard.press('ArrowRight');
	await page.keyboard.press('ArrowRight');
	await page.waitForTimeout(400);
	await page.keyboard.type('U');
	await expect.poll(() => highlighted(page), { message: 'typed after the arrow keys moved the caret into the highlight' }).toMatch(/^PaUragraph/);
	// Away and back by the keys picks the card again where it does not cover the text; on a phone typing and the keys never open the sheet.
	if (phone) await expect(sheet(page)).toHaveCount(0); else await shown();
}

test('@flow:doc-comment-edit-in-highlight with the margin column up, text in a highlight edits and its card is picked', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'The margin column is a desktop layout.');
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	const doc = await open(page, request);
	await editInsideHighlight(page, () => expect(card(page, doc, FIRST).locator('.ct.focused')).toHaveCount(1));
});

test('@flow:doc-comment-edit-in-highlight below the margin width, text in a highlight edits while its popover is open', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'The popover is the narrow canvas on a desktop.');
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 900, height: 900 });
	await open(page, request, { wide: false });
	await editInsideHighlight(page, () => expect(page.locator('[data-popover]')).toContainText(ITEMS[FIRST].body));
});

test('@flow:doc-comment-edit-in-highlight on a phone, a tap on a highlight opens its sheet, and once it is closed the text there edits', async ({ page, request }, info) => {
	test.skip(!info.project.name.includes('mobile'), 'The sheet is the phone layout.');
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 393, height: 852 });
	await open(page, request, { wide: false });
	await editInsideHighlight(page, () => expect(sheet(page)).toBeVisible(), true);
});
