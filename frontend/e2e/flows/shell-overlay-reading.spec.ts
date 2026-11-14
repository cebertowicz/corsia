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
	if (!await page.locator('.canvas-comment-drawer').count()) await page.getByRole('button', { name: '13 comments' }).click();
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
	// The first whole line is the first line that starts inside the view. Which line that is after the same scroll depends
	// on the engine's text metrics, so it is found, not named: a named line further down sits below a paragraph that
	// rewraps with the width, so it moves even while the first whole line holds.
	const firstWholeLine = () => scroller.evaluate((el) => {
		const w = window as unknown as { __firstLine?: Element };
		const top = el.getBoundingClientRect().top;
		w.__firstLine ??= [...el.querySelectorAll('.cm-line')].find((line) => line.getBoundingClientRect().top >= top - 0.5);
		return { y: w.__firstLine?.getBoundingClientRect().y ?? Number.NaN, drawn: !!w.__firstLine?.isConnected };
	});
	const before = await firstWholeLine();
	expect(before.drawn).toBe(true);
	await toggle.click();
	await page.waitForTimeout(1800);
	const open = await firstWholeLine();
	expect(open.drawn, 'the first whole line is still drawn while the panel is open').toBe(true);
	expect(Math.abs(open.y - before.y)).toBeLessThan(2);
	await toggle.click();
	await page.waitForTimeout(1800);
	const closed = await firstWholeLine();
	expect(closed.drawn, 'the first whole line is still drawn after the panel closes').toBe(true);
	expect(Math.abs(closed.y - before.y)).toBeLessThan(2);
});

// The column shown on load, with a reply being written in one of its cards: when the panel's trip leaves no room for the
// column, the list takes its narrow form (a popover on desktop) with that reply, rather than closing under it.
test('keeps the same reply and focus when the margin list stops fitting', async ({ page, request }) => {
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 1360, height: 900 });
	await seedDocument(page, request, true);
	const pane = page.locator('.pane');
	const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
	if (await pane.evaluate((el) => el.classList.contains('collapsed'))) await toggle.click();
	const drawer = page.locator('.canvas-comment-drawer.margin:not(.leaving)');
	if (!await drawer.count()) await page.getByRole('button', { name: '13 comments' }).click();
	const card = drawer.getByRole('region', { name: /^Comment by / }).first();
	if (!await card.locator('.ct-input').count()) await card.locator('.ct-head').click({ position: { x: 14, y: 10 } });
	const input = card.locator('.ct-input');
	await input.fill('A half-written reply');
	const id = await input.evaluate((el) => el.closest('[data-comment-id]')?.getAttribute('data-comment-id'));
	expect(id).toBeTruthy();
	await toggle.click();
	const reply = page.locator('.canvas-comment-drawer:not(.margin):not(.leaving)').locator(`[data-comment-id="${id}"] .ct-input`);
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
	if (await page.locator('.canvas-comment-drawer:not(.leaving)').count()) await page.getByRole('button', { name: '13 comments' }).click();
	await page.waitForTimeout(600);
}

/**
 * Waits out the header's passing Sent. At a narrow document width it wraps the header's controls onto a second line; when
 * it leaves, the document moves up by that line, so a press aimed at the text before then lands where the text no longer is.
 */
async function headerSettled(page: Page) {
	await expect(page.locator('.document-header .sending-now:has(.sent)')).toHaveCount(0, { timeout: 10_000 });
}

/** The three forms the reader can be writing in, per DocCommentSurface: the list, a thread pinned in a popover, a new draft. */
const writing = {
	list: {
		open: async (page: Page) => {
			const drawer = page.locator('.canvas-comment-drawer:not(.leaving)');
			if (!await drawer.count()) await page.getByRole('button', { name: '13 comments' }).click();
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
			if (!await card.count()) {
				await headerSettled(page);
				await page.locator('.cm-comment-anchor:visible').first().click();
			}
			await expect(card.first()).toBeVisible();
			return card.first();
		},
	},
	draft: {
		open: async (page: Page) => {
			await closeList(page);
			await headerSettled(page);
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
async function panelTrip(page: Page, request: Parameters<typeof seedDocument>[1], form: keyof typeof writing, width: number, by: 'mouse' | 'keyboard' | 'shortcut') {
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
	// Open the panel with the mouse, with its shortcut from the field, or by focusing the toggle and pressing Enter.
	if (by === 'mouse') {
		await toggle.click();
	} else if (by === 'shortcut') {
		await page.keyboard.press('ControlOrMeta+Backslash');
	} else {
		// Safari's Tab moves between fields only: from a reply it goes into the next thread's reply, which is the reader
		// choosing that field. Option-Tab is its key for every control, the way a keyboard reader reaches a button there.
		await page.keyboard.press(page.context().browser()?.browserType().name() === 'webkit' ? 'Alt+Tab' : 'Tab');
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
	if (by === 'shortcut') await page.keyboard.press('ControlOrMeta+Backslash');
	else await toggle.click();
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
		for (const by of ['mouse', 'keyboard', 'shortcut'] as const) {
			test(`keeps the ${form} comment form up through the panel, with its draft and caret, by ${by}, at ${width}`, async ({ page, request }) => {
				test.setTimeout(120_000);
				await panelTrip(page, request, form, width, by);
			});
		}
	}
}

/**
 * Tabbing on through the comments is the reader moving on: the panel's trip hands focus to no reply they only passed
 * through on the way, and their own draft stays where they left it.
 */
for (const width of [1360, 1100] as const) {
	test(`hands focus to no reply the reader tabbed past, at ${width}`, async ({ page, request }) => {
		test.setTimeout(120_000);
		await page.setViewportSize({ width, height: 900 });
		await seedDocument(page, request, true);
		const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
		if (!await page.locator('.pane').evaluate((el) => el.classList.contains('collapsed'))) await toggle.click();
		await page.waitForTimeout(1500);
		const input = await writing.list.open(page);
		await input.click();
		await input.pressSequentially('Half-typed');
		const before = await activeField(page);
		expect(before.tag, `the reply holds the caret before the walk (${JSON.stringify(before)})`).toBe('TEXTAREA');
		// Tab on until focus has just left another thread's reply: the last field passed is that one, not the reader's.
		let passed: string | null = null, onReply = false;
		for (let i = 0; i < 40 && !(passed && !onReply); i++) {
			await page.keyboard.press('Tab');
			const at = await page.evaluate(() => {
				const el = document.activeElement;
				return { reply: !!el?.matches('.ct-input'), of: el?.closest('[data-thread-id]')?.getAttribute('data-thread-id') ?? null };
			});
			if (at.reply && at.of && at.of !== before.of) passed = at.of;
			onReply = at.reply;
		}
		expect(passed && !onReply, `the walk passed another thread's reply (${passed}) and went on past it`).toBeTruthy();
		await toggle.focus();
		await page.keyboard.press('Enter');
		await page.waitForTimeout(1800);
		await toggle.focus();
		await page.keyboard.press('Enter');
		await page.waitForTimeout(2400);
		const after = await activeField(page);
		expect(after.tag === 'TEXTAREA' && after.of !== before.of, `focus went to another thread's reply after the trip (${JSON.stringify(after)})`).toBe(false);
		await expect(page.locator(`[data-thread-id="${before.of}"] .ct-input`).first()).toHaveValue('Half-typed');
	});
}

/** Writing on in the same reply while the panel is open: the caret comes back where the reader left it last, not at the open. */
test('brings the caret back where the reader left it while the panel was open', async ({ page, request }) => {
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 1360, height: 900 });
	await seedDocument(page, request, true);
	const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
	if (!await page.locator('.pane').evaluate((el) => el.classList.contains('collapsed'))) await toggle.click();
	await page.waitForTimeout(1500);
	const input = await writing.list.open(page);
	await input.click();
	await input.pressSequentially('Half-typed');
	await page.keyboard.press('ArrowLeft');
	await page.keyboard.press('ArrowLeft');
	const before = await activeField(page);
	await toggle.click();
	await page.waitForTimeout(600);
	await expect.poll(() => surfaceUp(page, before), { timeout: 15_000 }).toMatchObject({ found: true, up: true, clear: true });
	const reply = page.locator(`[data-thread-id="${before.of}"] .ct-input`).first();
	await reply.click();
	await page.keyboard.press('End');
	await page.keyboard.type(' more');
	await toggle.click();
	await page.waitForTimeout(600);
	await expect.poll(() => activeField(page), { timeout: 15_000 }).toMatchObject({ tag: 'TEXTAREA', of: before.of });
	await page.waitForTimeout(1800);
	const after = await activeField(page);
	expect(after.value).toBe('Half-typed more');
	expect(after.caret, `the caret is where the reader left it with the panel open (${JSON.stringify(after)})`).toBe('Half-typed more'.length);
});

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
	await headerSettled(page);
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

/**
 * A thread opened from the list, then the panel opened: the column comes beside the narrowed text and the line the thread
 * quotes stays in the view, not under the toolbar. The shell's move is slowed the way a busy computer slows it, which is
 * when the line used to be measured after it had already slid above the view. (Review 9 A, F2.)
 */
test('keeps an open thread\'s line in view when the panel opens on a slow move', async ({ page, request, browserName }) => {
	test.skip(browserName !== 'chromium', 'Slowing the move needs Chromium\'s animation controls.');
	test.setTimeout(180_000);
	await page.setViewportSize({ width: 1360, height: 900 });
	await seedDocument(page, request, true);
	const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
	if (!await page.locator('.pane').evaluate((el) => el.classList.contains('collapsed'))) await toggle.click();
	await page.waitForTimeout(1500);
	await closeList(page);
	await page.getByRole('button', { name: '13 comments' }).click();
	await page.waitForTimeout(1000);
	// The reader looks down the whole list and back up before opening the first thread.
	const listTo = (end: boolean) => page.locator('.canvas-comment-drawer:not(.leaving)').evaluate((drawer, end) => {
		const list = [drawer, ...drawer.querySelectorAll('*')].find((e) => e.scrollHeight > e.clientHeight + 2 && /auto|scroll/.test(getComputedStyle(e).overflowY)) as HTMLElement | undefined;
		if (list) list.scrollTop = end ? list.scrollHeight : 0;
	}, end);
	await listTo(true);
	await page.waitForTimeout(500);
	await listTo(false);
	await page.locator('.canvas-comment-drawer:not(.leaving) [data-comment-id]').first().locator('.ct-head').click({ position: { x: 14, y: 10 } });
	await page.waitForTimeout(800);
	const cdp = await page.context().newCDPSession(page);
	await cdp.send('Animation.enable');
	await cdp.send('Animation.setPlaybackRate', { playbackRate: 0.2 });
	await toggle.click();
	await expect.poll(() => page.locator('.canvas-comment-drawer.margin:not(.leaving)').count(), { timeout: 15_000 }).toBe(1);
	await page.waitForTimeout(2500);
	await cdp.send('Animation.setPlaybackRate', { playbackRate: 1 });
	const place = await page.evaluate(() => {
		const box = (e: Element | null | undefined) => { if (!e) return null; const b = e.getBoundingClientRect(); return { top: b.top, bottom: b.bottom }; };
		const scroller = box(document.querySelector('.cm-scroller'));
		const quoted = box([...document.querySelectorAll('.cm-comment-anchor')].find((e) => (e.textContent ?? '').includes('outstanding')));
		return { scroller, quoted };
	});
	expect(place.quoted, 'the quoted line is drawn').not.toBeNull();
	expect(place.quoted!.top >= place.scroller!.top - 1 && place.quoted!.bottom <= place.scroller!.bottom + 1, `the quoted line is inside the document's view (${JSON.stringify(place)})`).toBe(true);
});

/**
 * The panel's move redraws a card that is already up in whichever form the narrower canvas has room for. That form takes
 * no focus: the reader asked for the panel, not for a new surface, so focus stays on the panel's own control while it is
 * open, and the writing place returns only when the panel closes. (Review 6 findings F1, F2, F3.)
 */
test('takes no focus in a comment form the panel redrew, and keeps the card and its draft', async ({ page, request }) => {
	test.setTimeout(180_000);
	await page.setViewportSize({ width: 1360, height: 900 });
	await seedDocument(page, request, true);
	await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
	if (!await page.locator('.pane').evaluate((el) => el.classList.contains('collapsed'))) await toggle.click();
	await page.waitForTimeout(1500);
	const input = await writing.pin.open(page);
	await input.click();
	await input.pressSequentially('A reply held open');
	const before = await activeField(page);
	expect(before.tag, `the reply holds focus before the panel opens (${JSON.stringify(before)})`).toBe('TEXTAREA');
	await toggle.click();
	await page.waitForTimeout(1500);
	const open = await activeField(page);
	expect(open.tag, `the form the panel drew took focus while the panel was open (${JSON.stringify(open)})`).not.toBe('TEXTAREA');
	const stayed = await surfaceUp(page, before);
	expect(stayed.found, `the card is still up while the panel is open (${JSON.stringify(stayed)})`).toBe(true);
	expect(stayed.up, `the card the panel redrew is still on screen (${JSON.stringify(stayed)})`).toBe(true);
	expect(stayed.value, `the draft is unchanged while the panel is open (${JSON.stringify(stayed)})`).toBe(before.value);
	await toggle.click();
	await page.waitForTimeout(2200);
	const after = await activeField(page);
	expect(after.tag, `focus is back in the same reply after the panel closes (${JSON.stringify(after)})`).toBe('TEXTAREA');
	expect(after.of, `the reply is the same comment after the panel closes (${JSON.stringify(after)})`).toBe(before.of);
	expect(after.value, `the draft survived the trip (${JSON.stringify(after)})`).toBe(before.value);
});
