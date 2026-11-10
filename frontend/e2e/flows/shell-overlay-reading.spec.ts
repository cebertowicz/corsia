import { expect, test, type Page } from '../support/test';
import { seedDocument } from '../contact-sheet/document-comments';

test.use({ seedPerTest: true });

/** Where writing focus sits, and which comment surface form is up. */
const focusState = (page: Page, commentId: string | null = null) => page.evaluate((id) => {
	const el = document.activeElement;
	const area = el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement ? el : null;
	const card = el instanceof Element ? el.closest('[data-comment-id]') : null;
	return {
		focus: el ? `${el.tagName.toLowerCase()}${el.className ? `.${String(el.className).split(' ')[0]}` : ''}` : null,
		of: card?.getAttribute('data-comment-id') ?? null,
		inReplyOf: !!(area && area.classList.contains('ct-input') && (id === null || !!area.closest(`[data-comment-id="${id}"]`))),
		value: area?.value ?? null,
		caret: area?.selectionStart ?? null,
		sheet: !!document.querySelector('.canvas-comment-drawer.sheet'),
		margin: !!document.querySelector('.canvas-comment-drawer.margin'),
	};
}, commentId);

/** Opens the document's comments, focuses one thread's reply and leaves a half-typed draft in it. */
async function typeAReply(page: Page): Promise<{ id: string; caret: number }> {
	const list = page.locator('.canvas-comment-drawer').locator('[data-comment-id]').first();
	await list.locator('.ct-head').click();
	const input = list.locator('.ct-input');
	await expect(input).toBeVisible();
	await input.click();
	await input.pressSequentially('An unfinished reply');
	await page.keyboard.press('ArrowLeft');
	await page.keyboard.press('ArrowLeft');
	await page.keyboard.press('ArrowLeft');
	const caret = await input.evaluate((el) => el.selectionStart);
	const id = await list.getAttribute('data-comment-id');
	expect(id).toBeTruthy();
	return { id: id!, caret };
}

/** Focus a reply, open the Workspace panel, close it: the same reply has the draft and the caret again. */
async function keepsReplyFocusAcrossThePanel(page: Page, request: Parameters<typeof seedDocument>[1], width: number, collapseSidebar: boolean) {
	await page.setViewportSize({ width, height: 900 });
	await seedDocument(page, request, true);
	if (collapseSidebar) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	const pane = page.locator('.pane');
	const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
	if (!await pane.evaluate((el) => el.classList.contains('collapsed'))) await toggle.click();
	await page.waitForTimeout(1200);
	if (!await page.locator('.canvas-comment-drawer').count()) await page.getByRole('button', { name: '13 threads' }).click();
	const { id, caret } = await typeAReply(page);
	await toggle.click();
	await page.waitForTimeout(1800);
	await toggle.click();
	await page.waitForTimeout(1800);
	const after = await focusState(page, id);
	expect(after.inReplyOf, `the reply ${id} should have focus again, focus was ${after.focus} of ${after.of}`).toBe(true);
	expect(after.value).toBe('An unfinished reply');
	expect(after.caret).toBe(caret);
}

for (const [width, collapseSidebar] of [[1360, false], [1360, true], [1100, false], [1100, true]] as const) {
	test(`keeps a half-typed reply focused across the panel at ${width} sidebar ${collapseSidebar ? 'collapsed' : 'open'}`, async ({ page, request }) => {
		test.setTimeout(120_000);
		await keepsReplyFocusAcrossThePanel(page, request, width, collapseSidebar);
	});
}

test('keeps the first whole line while the panel opens and closes', async ({ page, request }) => {
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 1100, height: 900 });
	await seedDocument(page, request, true);
	const pane = page.locator('.pane');
	const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
	if (!await pane.evaluate((el) => el.classList.contains('collapsed'))) await toggle.click();
	const scroller = page.locator('.cm-scroller');
	await page.waitForTimeout(1800);
	await scroller.hover();
	await page.mouse.wheel(0, 1085);
	await expect.poll(() => scroller.evaluate((el) => el.scrollTop)).toBeGreaterThan(500);
	await page.waitForTimeout(600);
	const line = page.locator('.cm-line').filter({ hasText: 'Review section 2' }).first();
	await expect(line).toBeVisible();
	const before = (await line.boundingBox())!.y;
	await toggle.click();
	await page.waitForTimeout(1800);
	expect(Math.abs((await line.boundingBox())!.y - before)).toBeLessThan(2);
	await toggle.click();
	await page.waitForTimeout(1800);
	expect(Math.abs((await line.boundingBox())!.y - before)).toBeLessThan(2);
});

test('keeps the same reply and focus when a margin list becomes a sheet', async ({ page, request }) => {
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 1360, height: 900 });
	await seedDocument(page, request, true);
	const pane = page.locator('.pane');
	const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
	if (await pane.evaluate((el) => el.classList.contains('collapsed'))) await toggle.click();
	const drawer = page.locator('.canvas-comment-drawer.margin:not(.leaving)');
	if (!await drawer.count()) await page.getByRole('button', { name: '13 threads' }).click();
	const card = drawer.getByRole('region', { name: /^Comment by / }).first();
	if (!await card.locator('.ct-input').count()) await card.locator('.ct-head').click({ position: { x: 14, y: 10 } });
	const input = card.locator('.ct-input');
	await input.fill('A half-written reply');
	const id = await input.evaluate((el) => el.closest('[data-comment-id]')?.getAttribute('data-comment-id'));
	expect(id).toBeTruthy();
	await toggle.click();
	const reply = page.locator('.canvas-comment-drawer.sheet').locator(`[data-comment-id="${id}"] .ct-input`);
	await expect(reply).toHaveValue('A half-written reply');
	await expect(reply).toBeFocused();
});

/** Where writing focus sits, and the comment surface that holds it: a thread's card, the list, or the new-comment composer. */
const activeField = (page: Page) => page.evaluate(() => {
	const el = document.activeElement;
	const area = el instanceof HTMLTextAreaElement || el instanceof HTMLInputElement ? el : null;
	// A card is named by its thread; a card inside the list drawer is wrapped by the comment it shows.
	const of = (e: Element | null) => e?.closest('[data-thread-id]')?.getAttribute('data-thread-id') ?? e?.closest('[data-comment-id]')?.getAttribute('data-comment-id') ?? null;
	const box = (e: Element | null) => { if (!e) return null; const b = e.getBoundingClientRect(); return [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)] as [number, number, number, number]; };
	return {
		tag: el?.tagName ?? null,
		of: of(area ?? null),
		composer: !!area?.closest('.composer'),
		value: area?.value ?? null,
		caret: area?.selectionStart ?? null,
		box: box(area ?? null),
		panel: box(document.querySelector('.pane:not(.collapsed)')),
	};
});

/** Closes the thread list if the seed left it open: its drawer backdrop takes presses meant for the text. */
async function closeList(page: Page) {
	if (!await page.locator('.drawer-backdrop, .canvas-comment-drawer:not(.leaving)').count()) return;
	await page.keyboard.press('Escape');
	await expect.poll(() => page.locator('.drawer-backdrop').count(), { timeout: 6000 }).toBe(0);
	if (await page.locator('.canvas-comment-drawer:not(.leaving)').count()) await page.getByRole('button', { name: '13 threads' }).click();
	await page.waitForTimeout(600);
}

/** The three forms the reader can be writing in, per DocCommentSurface: the list, a thread pinned in a popover, a new draft. */
const writing = {
	list: {
		open: async (page: Page) => {
			const drawer = page.locator('.canvas-comment-drawer:not(.leaving)');
			if (!await drawer.count()) await page.getByRole('button', { name: '13 threads' }).click();
			const card = drawer.locator('[data-comment-id]').first();
			if (!await card.locator('.ct-input').count()) await card.locator('.ct-head').click({ position: { x: 14, y: 10 } });
			return card.locator('.ct-input');
		},
	},
	pin: {
		// The card for the thread whose text was clicked, in whichever form the width gives it: popover, margin column or sheet.
		open: async (page: Page) => {
			await closeList(page);
			const card = page.locator('[data-popover] .ct-input, .canvas-comment-drawer.margin .focused .ct-input, .canvas-comment-drawer.sheet .ct-input');
			// Press a highlight in the text: the thread it quotes opens as the card, in whichever form this width gives it.
			if (!await card.count()) await page.locator('.cm-comment-anchor:visible').first().click();
			await expect(card.first()).toBeVisible();
			return card.first();
		},
	},
	draft: {
		open: async (page: Page) => {
			await closeList(page);
			await page.locator('.cm-line').filter({ hasText: 'This document collects' }).first().dblclick({ position: { x: 40, y: 8 } });
			await page.locator('.sel-comment').click();
			const input = page.locator('.composer textarea').last();
			await expect(input).toBeVisible();
			return input;
		},
	},
};

/**
 * Is the surface the reader was writing in still up: the same comment's card or the same new-comment composer, holding
 * the same draft, on screen and clear of the open panel. A hidden popover stays in the DOM clipped away, so its own
 * inert and clip state is what says whether the reader can still see it.
 */
const surfaceUp = (page: Page, want: { of: string | null; composer: boolean }) => page.evaluate((want) => {
	type Rect = [number, number, number, number];
	const box = (e: Element | null): Rect | null => { if (!e) return null; const b = e.getBoundingClientRect(); return [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)]; };
	const panel = box(document.querySelector('.pane:not(.collapsed)'));
	const pane = document.querySelector('.pane:not(.collapsed)');
	for (const el of [...document.querySelectorAll<HTMLTextAreaElement>('.ct-input, .composer textarea')]) {
		const of = el.closest('[data-thread-id]')?.getAttribute('data-thread-id') ?? el.closest('[data-comment-id]')?.getAttribute('data-comment-id') ?? null;
		const composer = !!el.closest('.composer');
		if (want.composer ? !composer : of !== want.of) continue;
		const surface = el.closest('[data-popover], .canvas-comment-drawer, .drawer-panel');
		const css = surface ? getComputedStyle(surface) : null;
		const b = box(el);
		const up = !!surface && !surface.hasAttribute('inert') && surface.getAttribute('aria-hidden') !== 'true'
			&& !(css?.clipPath ?? '').includes('inset(100%)') && !!b && b[2] > 8 && b[3] > 8;
		const clear = !pane || !b || b[0] + b[2] <= pane.getBoundingClientRect().right - 1 || b[0] >= pane.getBoundingClientRect().right;
		return { found: true, up, value: el.value, box: b, panel, clear };
	}
	return { found: false, up: false, value: null, box: null, panel, clear: true };
}, want);

/**
 * A press on the panel toggle is not an outside press: whatever the reader is writing stays put through the trip, and
 * comes back with the same draft and caret at the same place in the comment. The card is expected to sit clear of the
 * panel while it is open, not under it.
 */
async function panelTrip(page: Page, request: Parameters<typeof seedDocument>[1], form: keyof typeof writing, width: number, by: 'mouse' | 'keyboard') {
	await page.setViewportSize({ width, height: 900 });
	await seedDocument(page, request, true);
	const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
	if (!await page.locator('.pane').evaluate((el) => el.classList.contains('collapsed'))) await toggle.click();
	await page.waitForTimeout(1500);
	const input = await writing[form].open(page);
	await input.click();
	await input.pressSequentially('Half-typed');
	await page.keyboard.press('ArrowLeft');
	await page.keyboard.press('ArrowLeft');
	const before = await activeField(page);
	expect(before.tag, `the ${form} form should hold the caret before the panel opens (${JSON.stringify(before)})`).toBe('TEXTAREA');
	expect(before.panel, `the panel is closed before the trip (${JSON.stringify(before)})`).toBeNull();
	// Open the panel with the mouse, or by focusing the toggle and pressing Enter from the field.
	if (by === 'mouse') {
		await toggle.click();
	} else {
		await page.keyboard.press('Tab');
		const reached = await page.evaluate(() => document.activeElement?.getAttribute('aria-label') === 'Toggle panel');
		if (!reached) await toggle.focus(); // The toggle is not in the tab order from these fields; a keyboard press still reaches it.
		await page.keyboard.press('Enter');
	}
	// The panel slides on a transition and the surface settles after it, so wait for the state instead of guessing a duration.
	await page.waitForTimeout(600);
	await expect.poll(() => surfaceUp(page, before), { timeout: 15_000, message: `the ${form} form stays up, clear of the panel, while the panel is open` })
		.toMatchObject({ found: true, up: true, clear: true });
	const openState = await surfaceUp(page, before);
	expect(openState.value, `the ${form} draft is unchanged while the panel is open (${JSON.stringify(openState)})`).toBe(before.value);
	await toggle.click();
	await page.waitForTimeout(600);
	await expect.poll(() => activeField(page), { timeout: 15_000, message: `focus, and the same ${form} field with its draft, are back after the panel closes` })
		.toMatchObject({ tag: 'TEXTAREA', of: before.of, composer: before.composer });
	// The shell is still easing and the surface may still settle into another form under the writing: the place has to
	// hold there, not merely land once, so read it again after the move is over.
	await page.waitForTimeout(1800);
	const after = await activeField(page);
	expect(after.tag, `focus is back in the ${form} form after the panel closes (${JSON.stringify(after)})`).toBe('TEXTAREA');
	expect(after.of, `the ${form} form is the same comment after the panel closes (${JSON.stringify(after)})`).toBe(before.of);
	expect(after.composer, `the ${form} form is the same composer after the panel closes (${JSON.stringify(after)})`).toBe(before.composer);
	expect(after.value, `the ${form} draft survived the panel trip (${JSON.stringify(after)})`).toBe(before.value);
	expect(after.caret, `the ${form} caret survived the panel trip (${JSON.stringify(after)})`).toBe(before.caret);
}

for (const form of ['list', 'pin', 'draft'] as const) {
	for (const width of [1360, 1100] as const) {
		for (const by of ['mouse', 'keyboard'] as const) {
			test(`keeps the ${form} comment form up through the panel, with its draft and caret, by ${by}, at ${width}`, async ({ page, request }) => {
				test.setTimeout(120_000);
				await panelTrip(page, request, form, width, by);
			});
		}
	}
}

/** The reader leaving the surface to write is moving on: the panel's close does not pull the surface back. */
test('leaves focus in the document when the reader clicks into the text as the panel closes', async ({ page, request }) => {
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 1360, height: 900 });
	await seedDocument(page, request, true);
	const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
	if (!await page.locator('.pane').evaluate((el) => el.classList.contains('collapsed'))) await toggle.click();
	await page.waitForTimeout(1500);
	const input = await writing.pin.open(page);
	await input.click();
	await input.fill('A reply the reader leaves behind');
	await toggle.click();
	await page.waitForTimeout(1800);
	await toggle.click();
	// The panel is easing closed: the reader goes back to the text on purpose.
	await page.waitForTimeout(250);
	await page.locator('.cm-line', { hasText: 'This document collects' }).first().click({ position: { x: 12, y: 8 } });
	await page.waitForTimeout(2200);
	const after = await activeField(page);
	expect(after.tag, `focus stays in the document text, not pulled back to the reply (${JSON.stringify(after)})`).not.toBe('TEXTAREA');
	expect(await page.evaluate(() => !!document.activeElement?.closest('.cm-content'))).toBe(true);
});

/**
 * Where a comment's quoted text and its composer sit: the word inside the document's visible rect, the composer beside
 * it. The width change is the hard case — the document rewraps, and the line the reader is writing about is the place
 * that has to be held, or the composer ends clamped at the canvas's edge with its word below the fold.
 */
const anchorPlace = (page: Page) => page.evaluate(() => {
	type Rect = [number, number, number, number];
	const box = (e: Element | null): Rect | null => { if (!e) return null; const b = e.getBoundingClientRect(); return [Math.round(b.x), Math.round(b.y), Math.round(b.width), Math.round(b.height)]; };
	const scroller = box(document.querySelector('.cm-scroller'));
	const word = box(document.querySelector('.cm-comment-pending'));
	const composer = box([...document.querySelectorAll('.composer')].find((c) => c.querySelector('textarea')) ?? null);
	const inside = (a: Rect | null, b: Rect | null) => !!a && !!b && a[0] >= b[0] - 1 && a[1] >= b[1] - 1 && a[0] + a[2] <= b[0] + b[2] + 1 && a[1] + a[3] <= b[1] + b[3] + 1;
	// The gap between the two rects, whichever side separates them; 0 when they touch or overlap.
	const gap = (a: Rect | null, b: Rect | null) => (!a || !b ? Number.NaN
		: Math.max(Math.max(b[1] - (a[1] + a[3]), a[1] - (b[1] + b[3])), Math.max(b[0] - (a[0] + a[2]), a[0] - (b[0] + b[2])), 0));
	return { scroller, word, composer, wordInsideScroller: inside(word, scroller), composerToWord: gap(composer, word) };
});

test('keeps the quoted word in view with its composer beside it across the panel and a width change', async ({ page, request }) => {
	test.setTimeout(240_000);
	await page.setViewportSize({ width: 1360, height: 900 });
	await seedDocument(page, request, true);
	const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
	if (!await page.locator('.pane').evaluate((el) => el.classList.contains('collapsed'))) await toggle.click();
	await page.waitForTimeout(1500);
	await closeList(page);
	// A comment on a line near the bottom of the view, where a reflow has room to push it out of sight.
	const scroller = page.locator('.cm-scroller');
	const line = page.locator('.cm-line', { hasText: 'The team can compare the layout' });
	await scroller.hover();
	await page.mouse.wheel(0, 900);
	await page.waitForTimeout(500);
	const rows = await line.evaluateAll((els) => els.map((e) => Math.round(e.getBoundingClientRect().y)));
	await line.nth(Math.max(rows.findIndex((y) => y > 650 && y < 790), 0)).dblclick({ position: { x: 40, y: 8 } });
	await page.locator('.sel-comment').click();
	const input = page.locator('.composer textarea').last();
	await expect(input).toBeVisible();
	await input.fill('A comment near the bottom');
	const typed = await anchorPlace(page);
	expect(typed.wordInsideScroller, `the quoted word is on screen while it is being written (${JSON.stringify(typed)})`).toBe(true);
	expect(typed.composerToWord, `the composer sits beside its word (${JSON.stringify(typed)})`).toBeLessThanOrEqual(40);
	await toggle.click();
	await page.waitForTimeout(1800);
	await page.setViewportSize({ width: 1100, height: 900 });
	await page.waitForTimeout(1500);
	await toggle.click();
	await page.waitForTimeout(2200);
	const after = await anchorPlace(page);
	expect(after.word, 'the composer still quotes a word').not.toBeNull();
	expect(after.wordInsideScroller, `the quoted word is still inside the document's visible rect after the width change (${JSON.stringify(after)})`).toBe(true);
	expect(after.composerToWord, `the composer sits beside its word after the width change (${JSON.stringify(after)})`).toBeLessThanOrEqual(40);
	await page.setViewportSize({ width: 1360, height: 900 });
	await page.waitForTimeout(1500);
	const back = await anchorPlace(page);
	expect(back.wordInsideScroller, `the quoted word is still on screen back at the original width (${JSON.stringify(back)})`).toBe(true);
	expect(back.composerToWord, `the composer is still beside its word back at the original width (${JSON.stringify(back)})`).toBeLessThanOrEqual(40);
});
