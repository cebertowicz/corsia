import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { ITEMS, FIRST, card, open, widen } from '../support/marginCards';

/**
 * Text under a comment's highlight is the document's text, so it edits like any other: a click or tap into it, or the arrow keys,
 * put the caret there, and typing, Backspace, Delete and paste change it while the comment stays on it. The card the caret
 * picks still opens without taking the keystrokes: the margin card beside the text; below the margin width, the popover a click
 * opens, which closes on the first key so it never covers what is typed; on a phone, the sheet a tap opens.
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

const popover = (page: Page) => page.locator('[data-popover]');

/**
 * Puts the caret in the highlight as the person does and checks the card opened (`shown`), then edits there. On a phone
 * the tap places the caret and opens the sheet, which covers the text; Close returns to typing there.
 * Below the margin width the click opens the popover, and the first key typed closes it.
 */
async function editInsideHighlight(page: Page, shown: () => Promise<void>, mode: 'margin' | 'popover' | 'phone' = 'margin') {
	const phone = mode === 'phone';
	await expect(highlight(page)).toHaveText(QUOTE);
	if (phone) await highlight(page).tap(); else await highlight(page).click();
	await shown();
	if (phone) {
		await sheet(page).getByRole('button', { name: 'Close', exact: true }).click();
		await expect(sheet(page)).toHaveCount(0);
	}
	// The card opens a beat after the caret lands: what is typed after it must still reach the text.
	await page.waitForTimeout(400);
	if (phone) await expect(sheet(page), 'Close returns to typing at the tapped place').toHaveCount(0);
	await expect.poll(() => focusIn(page), { message: 'the editor keeps the focus' }).toBe('editor');
	await page.keyboard.type('QWE');
	await expect.poll(() => highlighted(page), { message: 'typed text lands inside the highlight, which stays on it' }).toContain('QWE');
	if (mode === 'popover') await expect(popover(page), 'the first key typed closes the popover over the text').toHaveCount(0);
	await page.keyboard.press('ArrowLeft');
	await page.keyboard.press('ArrowLeft');
	await page.keyboard.press('Backspace');
	await page.keyboard.press('Delete');
	if (mode === 'margin') await shown();
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
	// Away and back by the keys picks the card again where it sits beside the text; where it would cover the text (popover, sheet) the keys and typing open none.
	if (phone) await expect(sheet(page)).toHaveCount(0); else if (mode === 'popover') await expect(popover(page)).toHaveCount(0); else await shown();
}

test('@flow:doc-comment-edit-in-highlight with the margin column up, text in a highlight edits and its card is picked', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'The margin column is a desktop layout.');
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	const doc = await open(page, request);
	await editInsideHighlight(page, () => expect(card(page, doc, FIRST).locator('.ct.focused')).toHaveCount(1));
});

test('@flow:doc-comment-edit-in-highlight below the margin width, a click opens the popover, the first key closes it, and text in the highlight edits', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'The popover is the narrow canvas on a desktop.');
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 900, height: 900 });
	await open(page, request, { wide: false });
	await editInsideHighlight(page, () => expect(popover(page)).toContainText(ITEMS[FIRST].body), 'popover');
});

const GONE = 'This text is no longer in the document';

/** The caret at the start of the first highlight's line, where the highlight begins (the first comment's quote starts its paragraph). */
async function caretAtLineStart(page: Page) {
	await highlight(page).click();
	await expect.poll(() => focusIn(page)).toBe('editor');
	await page.keyboard.press('Home');
	await page.keyboard.press('Home');
}

/** Selects the first highlight's whole text from its first character, `skip` characters into the line, by the keys. */
async function selectHighlight(page: Page, skip = 0) {
	await caretAtLineStart(page);
	for (let i = 0; i < skip; i++) await page.keyboard.press('ArrowRight');
	for (let i = 0; i < QUOTE.length; i++) await page.keyboard.press('Shift+ArrowRight');
	await expect.poll(() => page.evaluate(() => getSelection()?.toString())).toBe(QUOTE);
}

/** Reloads the page once the saved file holds `saved`, and opens the document again with its column. */
async function reloadOnceSaved(page: Page, doc: Awaited<ReturnType<typeof open>>, saved: string) {
	await expect.poll(() => doc.storedText(), { message: 'the edit is saved before the reload', timeout: 15_000 }).toContain(saved);
	await page.reload();
	await page.locator('.thread-item').filter({ hasText: 'suggestions-owner-' }).first().click();
	await expect(page.locator('.cm-scroller')).toBeVisible();
	await widen(page, doc);
}

test('@flow:doc-comment-edit-in-highlight text typed at either edge of a highlight lands outside it, and typing over all of it keeps the comment through a reload', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'The margin column is a desktop layout.');
	test.setTimeout(150_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	const doc = await open(page, request);
	// At the first character: the caret before it, so what is typed goes before the highlight.
	await caretAtLineStart(page);
	await page.keyboard.type('A');
	await expect.poll(() => highlighted(page), { message: 'typed at the first character, outside the highlight' }).toBe(QUOTE);
	await expect(paragraph(page)).toContainText(`A${QUOTE} says`);
	// At the last character: the caret after it, so what is typed goes after the highlight.
	for (let i = 0; i < QUOTE.length; i++) await page.keyboard.press('ArrowRight');
	await page.keyboard.type('B');
	await expect.poll(() => highlighted(page), { message: 'typed at the last character, outside the highlight' }).toBe(QUOTE);
	await expect(paragraph(page)).toContainText(`A${QUOTE}B says`);
	// The whole highlight selected and typed over: the comment moves onto what was typed.
	await selectHighlight(page, 1);
	await page.keyboard.type('Handoff overview');
	await expect.poll(() => highlighted(page), { message: 'typing over the whole highlight keeps the comment on the typed text' }).toBe('Handoff overview');
	await expect(card(page, doc, FIRST)).not.toContainText(GONE);
	await reloadOnceSaved(page, doc, 'AHandoff overviewB says');
	await expect.poll(() => highlighted(page), { message: 'the comment stays on the typed text after a reload' }).toBe('Handoff overview');
	await expect(card(page, doc, FIRST)).toBeVisible();
	await expect(card(page, doc, FIRST)).not.toContainText(GONE);
});

test('@flow:doc-comment-edit-in-highlight deleting all of a highlight with nothing typed leaves its comment with the text gone', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'The margin column is a desktop layout.');
	test.setTimeout(150_000);
	await page.setViewportSize({ width: 1440, height: 900 });
	const doc = await open(page, request);
	await selectHighlight(page);
	await page.keyboard.press('Backspace');
	await expect(card(page, doc, FIRST)).toContainText(GONE);
	await reloadOnceSaved(page, doc, `# Handoff plan\n\n says that agents`);
	await expect(card(page, doc, FIRST)).toContainText(GONE);
});

test('@flow:doc-comment-edit-in-highlight on a phone, a key typed with the sheet up closes it and lands at the tapped place; Escape closes it and keeps the caret', async ({ page, request }, info) => {
	test.skip(!info.project.name.includes('mobile'), 'The sheet is the phone layout.');
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 393, height: 852 });
	await open(page, request, { wide: false });
	await highlight(page).tap();
	await expect(sheet(page)).toBeVisible();
	await page.keyboard.type('Z');
	await expect(sheet(page), 'the first key typed closes the sheet').toHaveCount(0);
	await expect.poll(() => highlighted(page), { message: 'the key lands at the tapped place, inside the highlight' }).toContain('Z');
	expect((await highlighted(page)).replace('Z', '')).toBe(QUOTE);
	await expect.poll(() => focusIn(page)).toBe('editor');
	await highlight(page).tap();
	await expect(sheet(page)).toBeVisible();
	await page.keyboard.press('Escape');
	await expect(sheet(page), 'Escape closes the sheet').toHaveCount(0);
	await expect.poll(() => focusIn(page), { message: 'Escape gives the text its caret back' }).toBe('editor');
	await page.keyboard.type('Y');
	await expect.poll(() => highlighted(page), { message: 'typed after Escape lands at the tapped place' }).toContain('Y');
	expect((await highlighted(page)).replace(/[ZY]/g, '')).toBe(QUOTE);
});

test('@flow:doc-comment-edit-in-highlight on a phone, a tap on a highlight places the caret and opens its sheet, and Close returns to typing there', async ({ page, request }, info) => {
	test.skip(!info.project.name.includes('mobile'), 'The sheet is the phone layout.');
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 393, height: 852 });
	await open(page, request, { wide: false });
	await editInsideHighlight(page, () => expect(sheet(page)).toBeVisible(), 'phone');
});
