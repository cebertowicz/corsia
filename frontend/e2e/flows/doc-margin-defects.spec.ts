import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { ITEMS, BRAVO, FIRST, SUGGESTION2, card, geometry, open, pill, type Doc } from '../support/marginCards';

/**
 * Margin comments, the defects found in the built column: a picked card lets go on a click into plain text and on Escape; the
 * picked card's Send reply stays reachable at the bottom of the window; a Resolve refused in the column is shown on the card;
 * and a narrow desktop width that is not a phone opens the popover, not the sheet.
 */

test.beforeEach(async ({ page }, info) => {
	if (info.project.name === 'chromium-desktop') await page.setViewportSize({ width: 1440, height: 900 });
	await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' });
});

const tops = async (page: Page, doc: Doc) => (await geometry(page, doc.ids)).map((row) => (row.card ? Math.round(row.card.top) : null));

test('@flow:doc-margin-defects a picked card returns to rest on a click into plain text and on Escape', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'Desktop layout.');
	test.setTimeout(120_000);
	const doc = await open(page, request);
	await expect.poll(async () => (await tops(page, doc)).filter((top) => top !== null).length).toBe(ITEMS.length);
	await page.waitForTimeout(600);
	const rest = await tops(page, doc);
	const settled = () => expect.poll(() => tops(page, doc), { message: 'cards back on their resting tops' }).toEqual(rest);
	const pick = async () => {
		await card(page, doc, BRAVO).locator('.ct-head').click({ position: { x: 14, y: 10 } });
		await expect(card(page, doc, BRAVO).locator('.ct.focused')).toHaveCount(1);
		await expect.poll(() => tops(page, doc), { message: 'the pick moves its neighbours' }).not.toEqual(rest);
	};
	// The pick moves its neighbours; a click into text no comment covers lets it go.
	await pick();
	await page.locator('.cm-line', { hasText: 'Paragraph 6 of the plan' }).first().click({ position: { x: 40, y: 8 } });
	await expect(page.locator('.ct.focused')).toHaveCount(0);
	await expect(page.locator('.cm-comment-focused')).toHaveCount(0);
	await settled();
	await expect(page.locator('.canvas-comment-drawer.margin'), 'the column stays up').toBeVisible();
	// Escape from inside the card, then from the text, lets it go the same way.
	await pick();
	await page.keyboard.press('Escape');
	await expect(page.locator('.ct.focused')).toHaveCount(0);
	await settled();
	await page.locator('.cm-comment-anchor', { hasText: 'bravo bravox' }).first().click();
	await expect(card(page, doc, BRAVO).locator('.ct.focused')).toHaveCount(1);
	await page.keyboard.press('Escape');
	await expect(page.locator('.ct.focused')).toHaveCount(0);
	await settled();
	await expect(page.locator('.canvas-comment-drawer.margin'), 'the column stays up').toBeVisible();
});

test('@flow:doc-margin-defects Send reply of a card near the bottom of the window can be reached, and a refused Resolve stays on its card', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'Desktop layout.');
	test.setTimeout(120_000);
	const doc = await open(page, request);
	// Paragraph 8 sits at the bottom edge of the window: put its line there, then pick its suggestion card and type a reply.
	await page.locator('.cm-line', { hasText: 'Paragraph 8 of' }).first().evaluate((line) => {
		const scroller = document.querySelector('.cm-scroller')!;
		scroller.scrollTop += line.getBoundingClientRect().top - (window.innerHeight - 140);
	});
	await page.waitForTimeout(600);
	const wrap = card(page, doc, SUGGESTION2);
	await wrap.locator('.ct-head').click({ position: { x: 14, y: 10 } });
	await wrap.locator('textarea').fill('Is the intro the right place?');
	const send = wrap.getByRole('button', { name: 'Send reply' });
	await expect.poll(async () => {
		const [box, canvas, columnScroll] = [await send.boundingBox(), await page.locator('.cm-scroller').boundingBox(), await page.locator('.drawer-comments.in-margin').evaluate((el) => el.scrollTop)];
		return !!box && !!canvas && box.y + box.height <= canvas.y + canvas.height + 0.5 && columnScroll === 0;
	}, { message: 'Send reply inside the visible document, and the column itself not scrolled' }).toBe(true);
	await send.click();
	await expect(wrap).toContainText('Is the intro the right place?');
	// A suggestion with a reply stays an open conversation once decided: accept it, then Resolve it with the server refusing.
	await wrap.getByRole('button', { name: 'Accept', exact: true }).click();
	await expect(wrap.locator('.ct-decided')).toBeVisible();
	let held = 0;
	await page.route(new RegExp(`/api/comments/${doc.ids[SUGGESTION2]}(\\?|$)`), async (route) => {
		if (route.request().method() !== 'PATCH') return route.continue();
		held += 1;
		await new Promise((resolve) => setTimeout(resolve, 600));
		await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: 'refused' }) });
	});
	await wrap.locator('.ct-act.resolve').click();
	await expect(wrap.locator('.ct-decision-error')).toBeVisible();
	await expect(wrap.locator('.ct-act.resolve'), 'the card stays, Resolve can be tried again').toBeVisible();
	expect(held).toBe(1);
});

test('@flow:doc-margin-defects the column closed and reopened at once keeps its room beside the text', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'Desktop layout.');
	test.setTimeout(120_000);
	const doc = await open(page, request);
	await page.waitForTimeout(600);
	const room = () => page.locator('.cm-scroller').evaluate((el) => Number.parseFloat(getComputedStyle(el).paddingRight));
	const shown = await room();
	expect(shown, 'the editor keeps room for the column').toBeGreaterThan(200);
	// Reopened while the column is still fading out: the same section comes back.
	await pill(page).click();
	await pill(page).click();
	await expect(card(page, doc, FIRST)).toBeVisible();
	await page.waitForTimeout(600);
	await expect(page.locator('.canvas-comment-drawer.margin.leaving')).toHaveCount(0);
	expect(await room(), 'the room is back, so no card sits over the text').toBe(shown);
	const [text, first] = [await page.locator('.cm-line', { hasText: 'Paragraph 1 of the plan' }).first().boundingBox(), await card(page, doc, FIRST).boundingBox()];
	expect(first!.x, 'the card starts right of the text').toBeGreaterThanOrEqual(text!.x + text!.width);
});

test('@flow:doc-margin-defects a narrow desktop width opens the popover at the click, and a phone the sheet', async ({ page, request }, info) => {
	test.setTimeout(120_000);
	const mobile = info.project.name.includes('mobile');
	await page.setViewportSize(mobile ? { width: 393, height: 852 } : { width: 1000, height: 800 });
	const doc = await open(page, request, { wide: false });
	await page.waitForTimeout(600);
	expect(await page.locator('[data-margin-card]').count()).toBe(0);
	await page.locator('.cm-comment-anchor', { hasText: 'Paragraph 1 of the plan' }).first().click();
	if (mobile) {
		await expect(page.locator('[data-adaptive-drawer] .drawer-panel')).toBeVisible();
	} else {
		await expect(page.locator('[data-popover]')).toBeVisible();
		await expect(page.locator('[data-adaptive-drawer]')).toHaveCount(0);
		await page.keyboard.press('Escape');
		await expect(page.locator('[data-popover]')).toHaveCount(0);
		await expect(page.locator('.cm-scroller'), 'Escape closes the card, keeping the document open').toBeVisible();
	}
	expect(doc.ids.length).toBe(ITEMS.length);
});
