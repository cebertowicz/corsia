import { test } from '../support/test';
import fs from 'node:fs';
import path from 'node:path';
import { execFileSync } from 'node:child_process';
import { expect, type Page, type APIRequestContext } from '@playwright/test';
import { openSuggestionDocument } from '../support/docSuggestions';
import { TEST_TOKEN } from '../support/corsia';

/**
 * Margin comments: open threads sit as cards beside their text, a cluster stacks, a picked card keeps its own line and pushes its
 * neighbours aside, a thread whose text is gone stays at its nearest line, each suggestion is its own card, the drawer lists only
 * resolved threads (newest first), and where the canvas is too narrow for the column the thread opens as a popover at the click
 * (on a phone, the sheet). Replaces the marker specs: there is no margin icon to press any more.
 */

const para = (n: number) => `Paragraph ${n} of the plan says that agents hand work to each other, and every handoff should carry the intent behind it so the next owner never has to guess what was meant by the person who asked.`;
const FIVE = 'Paragraph 5 notes alpha alphax and bravo bravox so that two notes sit on one line, then it carries on for a while so that the paragraph wraps onto a few lines of the page.';
const TEXT = `# Handoff plan\n\n${[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((i) => (i === 5 ? FIVE : para(i))).join('\n\n')}\n`;
// A comment, two comments on one line (the cluster), a suggestion, a suggestion, a comment, in document order.
// A skill file must load as a skill, so the history document opens with its front matter.
const SKILL_TEXT = `---\nname: demo\ndescription: A test skill.\n---\n\n${TEXT}`;
const ITEMS = [
	{ quote: 'Paragraph 1 of the plan', body: 'Which agents? Name them here.' },
	{ quote: 'alpha alphax', body: 'Alpha needs a source.' },
	{ quote: 'bravo bravox', body: 'Same for bravo.' },
	{ quote: 'Paragraph 10 of the plan', body: 'This contradicts the intro. Pick one.' },
	{ quote: 'Paragraph 3 of the plan', replacement: 'Paragraph 3 of the rollout plan', body: 'Clearer.' },
	{ quote: 'Paragraph 8 of the plan', replacement: 'Paragraph 8 of the intro', body: 'Match the intro.' },
];
const [FIRST, ALPHA, BRAVO, LAST, SUGGESTION] = [0, 1, 2, 3, 4];

type Doc = Awaited<ReturnType<typeof openSuggestionDocument>>;
const pill = (page: Page) => page.getByRole('button', { name: /^\d+ (open )?threads?/i }).first();
const card = (page: Page, doc: Doc, i: number) => page.locator(`[data-margin-card="${doc.ids[i]}"]`);
const docWidth = (page: Page) => page.evaluate(() => Math.round(document.querySelector('.doc')?.getBoundingClientRect().width ?? 0));

/** Folds the sidebar and the Workspace panel away until the canvas has room for the column, then shows the column. */
async function widen(page: Page, doc: Doc) {
	for (const name of ['Toggle sidebar', 'Toggle panel']) {
		if ((await docWidth(page)) >= 800) break;
		await page.getByRole('button', { name, exact: true }).click();
		await expect.poll(() => docWidth(page)).toBeGreaterThan(600);
	}
	if (!(await page.locator('[data-margin-card]').count())) await pill(page).click();
	await expect(page.locator('.canvas-comment-drawer.margin')).toBeVisible();
	return doc;
}

/** Seeds the document and, on a desktop, widens the canvas so it has room for the column. */
async function open(page: Page, request: APIRequestContext, { wide = true }: { wide?: boolean } = {}) {
	const doc = await openSuggestionDocument(page, request, TEXT, ITEMS, { splitWidth: wide ? 5000 : 540 });
	await expect(page.locator('.cm-scroller')).toBeVisible();
	if (wide) {
		await widen(page, doc);
		await expect(card(page, doc, FIRST)).toBeVisible();
	}
	return doc;
}

/** Top of the first drawn row of a thread's highlight (comment) or struck text (suggestion), and of its card. */
const geometry = (page: Page, ids: string[]) => page.evaluate((all) => all.map((id) => {
	const wrap = document.querySelector<HTMLElement>(`[data-margin-card="${id}"]`);
	const mark = [...document.querySelectorAll<HTMLElement>('.cm-comment-line, .cm-suggestion-del, .cm-suggestion-insert')]
		.find((node) => (node.dataset.commentIds ?? node.dataset.suggestionId ?? '').split(',').includes(id));
	const c = wrap?.getBoundingClientRect(), m = mark?.getBoundingClientRect();
	return { id, card: c ? { top: c.top, bottom: c.bottom, left: c.left } : null, line: m ? m.top : null };
}), ids);

test.beforeEach(async ({ page }, info) => {
	if (info.project.name === 'chromium-desktop') await page.setViewportSize({ width: 1440, height: 900 });
	await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'no-preference' });
});

test('@flow:doc-margin-cards every open thread is a card beside its text, a cluster stacks, and no icon stands in the left margin', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'The column is a desktop layout; the phone keeps its sheet (below).');
	test.setTimeout(120_000);
	const doc = await open(page, request);
	for (let i = 0; i < doc.ids.length; i++) await expect(card(page, doc, i)).toHaveCount(1);
	// No margin marker and no pressable control in the rail the content padding reserves.
	expect(await page.locator('.cm-comment-marker, .cm-comment-rail button').count()).toBe(0);
	// Visible cards never overlap one another and keep document order.
	const rects = (await geometry(page, doc.ids)).filter((row) => row.card).map((row) => row.card!);
	const sorted = [...rects].sort((a, b) => a.top - b.top);
	for (let i = 1; i < sorted.length; i++) expect(sorted[i].top, 'cards do not overlap').toBeGreaterThanOrEqual(sorted[i - 1].bottom - 0.5);
	// The first comment is alone on its line: its card sits level with the text.
	const [first] = await geometry(page, doc.ids);
	expect(Math.abs(first.card!.top - first.line!), 'a lone card sits beside its line').toBeLessThanOrEqual(12);
	// Alpha and bravo share a line: bravo stacks below alpha instead of covering it.
	const [, alpha, bravo] = await geometry(page, doc.ids);
	expect(bravo.card!.top).toBeGreaterThanOrEqual(alpha.card!.bottom - 0.5);
	expect(alpha.card!.left).toBeCloseTo(bravo.card!.left, 0);
	// The stack rule, for every card at rest taken in order down the column: it sits level with its line (the lone first card shows the offset between a card's top and its line),
	// or, where the card above reaches that line, directly under it with the 8 px gap (placeCards' GAP).
	const offset = first.card!.top - first.line!;
	// The column places cards as the text lays out, so the rule is read once they have settled.
	await expect.poll(async () => {
		const rows = (await geometry(page, doc.ids)).filter((row) => row.card && row.line != null).sort((a, b) => a.card!.top - b.card!.top);
		if (rows.length !== doc.ids.length) return Infinity;
		return Math.max(...rows.map((row, i) => Math.abs(row.card!.top - Math.max(row.line! + offset, i ? rows[i - 1].card!.bottom + 8 : -Infinity))));
	}, { message: 'every card level with its line, or directly under the card above it' }).toBeLessThan(0.5);
	// At rest a card is the comment, its replies and a suggestion's diff and buttons: no reply field and no quote line until it is picked.
	for (const id of doc.ids) {
		await expect(page.locator(`[data-margin-card="${id}"] textarea`), 'no reply field on a card at rest').toHaveCount(0);
		await expect(page.locator(`[data-margin-card="${id}"] .quote`), 'no quote line on a card at rest').toHaveCount(0);
	}
});

test('@flow:doc-margin-cards a picked card keeps its own line, the cards around it step aside, and the text pick selects the card', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'Desktop layout.');
	test.setTimeout(120_000);
	const doc = await open(page, request);
	// Focusing a card adds its quote, replies and Reply field; the header (avatar, name, time) stays on its one row of the resting card.
	const headRow = () => card(page, doc, BRAVO).locator('.ct-head').evaluate((head) => {
		const top = (selector: string) => Math.round(head.querySelector(selector)!.getBoundingClientRect().top);
		return { height: Math.round(head.getBoundingClientRect().height), author: top('.ct-author'), time: top('.ct-at.full') };
	});
	const resting = await headRow();
	expect(Math.abs(resting.author - resting.time), 'name and time share a row at rest').toBeLessThanOrEqual(3);
	await card(page, doc, BRAVO).locator('.ct-head').click({ position: { x: 14, y: 10 } });
	await expect(card(page, doc, BRAVO).locator('.ct.focused')).toHaveCount(1);
	await expect.poll(async () => {
		const [, alpha, bravo] = await geometry(page, doc.ids);
		return Math.abs(bravo.card!.top - bravo.line!) <= 12 && alpha.card!.bottom <= bravo.card!.top + 0.5;
	}, { message: 'bravo level with its own line, alpha pushed above it' }).toBe(true);
	await expect.poll(headRow, { message: 'the focused header keeps its one row' }).toMatchObject({ height: resting.height });
	const focusedRow = await headRow();
	expect(Math.abs(focusedRow.author - focusedRow.time)).toBeLessThanOrEqual(3);
	// The highlight of the picked thread is deeper than the others.
	await expect(page.locator('.cm-comment-focused').first()).toBeVisible();
	// Picking highlighted text in the document picks its card, with no popover over the text.
	await page.locator('.cm-comment-anchor', { hasText: 'Paragraph 1 of the plan' }).first().click();
	await expect(card(page, doc, FIRST).locator('.ct.focused')).toHaveCount(1);
	await expect(page.locator('[data-popover]')).toHaveCount(0);
});

test('@flow:doc-margin-cards focusing a card below the visible column does not slide the cards off their lines', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'Desktop layout.');
	test.setTimeout(120_000);
	const doc = await open(page, request);
	await expect(card(page, doc, FIRST)).toBeVisible();
	// The lowest card reaches past the bottom of the column; moving focus into it must not scroll the column's clip, which would carry every card away from its text.
	const clipScroll = await page.evaluate(() => {
		const clip = document.querySelector<HTMLElement>('.drawer-comments.margin-clip')!;
		const lowest = [...document.querySelectorAll<HTMLElement>('[data-margin-card]')].sort((a, b) => b.getBoundingClientRect().bottom - a.getBoundingClientRect().bottom)[0];
		const overshoot = lowest.getBoundingClientRect().bottom - clip.getBoundingClientRect().bottom;
		lowest.querySelector<HTMLElement>('button, textarea, [contenteditable]')?.focus();
		return { overshoot, top: clip.scrollTop };
	});
	expect(clipScroll.overshoot, 'a card reaches below the column').toBeGreaterThan(0);
	expect(clipScroll.top, 'the clip did not scroll').toBe(0);
	const [first] = await geometry(page, doc.ids);
	expect(Math.abs(first.card!.top - first.line!)).toBeLessThanOrEqual(12);
});

test('@flow:doc-margin-cards each suggestion is its own card with Accept and Reject', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'Desktop layout.');
	test.setTimeout(120_000);
	const doc = await open(page, request);
	for (const i of [SUGGESTION, SUGGESTION + 1]) {
		await expect(card(page, doc, i)).toHaveCount(1);
		await expect(card(page, doc, i).getByRole('button', { name: 'Accept', exact: true })).toBeVisible();
		await expect(card(page, doc, i).getByRole('button', { name: 'Reject', exact: true })).toBeVisible();
	}
	await card(page, doc, SUGGESTION).getByRole('button', { name: 'Reject', exact: true }).click();
	await expect(card(page, doc, SUGGESTION)).toHaveCount(0);
});

test('@flow:doc-margin-cards a thread whose text was deleted keeps its card at the nearest line, marked', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'Desktop layout.');
	test.setTimeout(120_000);
	const doc = await open(page, request);
	await page.evaluate(() => {
		const content = document.querySelector<HTMLElement>('.cm-content')!;
		content.focus();
		const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
		for (let node = walker.nextNode(); node; node = walker.nextNode()) {
			const at = node.textContent?.indexOf('Paragraph 1 of the plan') ?? -1;
			if (at < 0) continue;
			const range = document.createRange(); range.setStart(node, at); range.setEnd(node, at + 'Paragraph 1 of the plan'.length);
			const selection = getSelection()!; selection.removeAllRanges(); selection.addRange(range);
			return;
		}
	});
	await page.keyboard.press('Backspace');
	await expect(card(page, doc, FIRST)).toHaveCount(1);
	await expect(card(page, doc, FIRST).locator('.ct.orphaned')).toHaveCount(1);
	await expect(card(page, doc, FIRST).locator('.ct-orphan')).toHaveText('This text is no longer in the document');
	expect(await page.locator('.margin-aside-orphans, .orphans-button').count()).toBe(0);
});

test('@flow:doc-margin-cards the drawer lists only resolved threads, newest resolved first', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'Desktop layout.');
	test.setTimeout(120_000);
	const doc = await open(page, request);
	for (const i of [ALPHA, FIRST, LAST]) {
		const resolved = await request.patch(`/api/comments/${doc.ids[i]}`, { params: doc.params, data: { resolved: true } });
		expect(resolved.ok(), await resolved.text()).toBeTruthy();
		// Resolution times are second-resolution: give each its own.
		await page.waitForTimeout(1100);
	}
	await page.reload();
	await page.locator('.thread-item').filter({ hasText: 'suggestions-owner-' }).first().click();
	await expect(page.locator('.cm-scroller')).toBeVisible();
	await widen(page, doc);
	// Resolved threads have left the column; the rest stay.
	await expect(card(page, doc, ALPHA)).toHaveCount(0);
	await expect(card(page, doc, BRAVO)).toHaveCount(1);
	await page.locator('.aside-button[data-aside-kind="resolved"]').click();
	const order = await page.locator('.margin-aside [data-comment-id]').evaluateAll((nodes) => nodes.map((node) => node.getAttribute('data-comment-id')));
	expect(order).toEqual([doc.ids[LAST], doc.ids[FIRST], doc.ids[ALPHA]]);
});

test('@flow:doc-margin-cards below the margin width a thread opens as a popover at the click, and on a phone as the sheet', async ({ page, request }, info) => {
	test.setTimeout(120_000);
	const mobile = info.project.name.includes('mobile');
	if (mobile) await page.setViewportSize({ width: 393, height: 852 });
	const doc = await open(page, request, { wide: false });
	await page.waitForTimeout(600);
	expect(await page.locator('[data-margin-card]').count()).toBe(0);
	const target = page.locator('.cm-comment-anchor', { hasText: 'Paragraph 1 of the plan' }).first();
	await target.click();
	if (mobile) {
		await expect(page.locator('[data-adaptive-drawer] .drawer-panel')).toBeVisible();
	} else {
		const popover = page.locator('[data-popover]');
		await expect(popover).toBeVisible();
		const [text, box] = [await target.boundingBox(), await popover.boundingBox()];
		// The popover is near the text that was pressed, not in a column.
		expect(Math.abs(box!.y - text!.y)).toBeLessThan(400);
		await expect(popover).toContainText(ITEMS[FIRST].body);
	}
	expect(doc.ids.length).toBe(ITEMS.length);
});

test('@flow:doc-margin-cards the column comes back when version history closes, however it is closed', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'Desktop layout.');
	test.setTimeout(150_000);
	// Two saved versions give History something to list.
	const doc = await openSuggestionDocument(page, request, SKILL_TEXT, ITEMS, {
		splitWidth: 5000,
		// History is offered for guidance files only (a skill's SKILL.md), recorded in the workspace's Git: put the document where a skill lives.
		placeAt: (ownerId, workspace) => {
			execFileSync('git', ['init', '-q', workspace]);
			const folder = path.join(workspace, '.threads', ownerId, 'skills', 'demo');
			fs.mkdirSync(folder, { recursive: true });
			return path.join('.threads', ownerId, 'skills', 'demo', 'SKILL.md');
		},
		beforeOpen: async ({ docPath, ownerId }) => {
			for (const text of [`${SKILL_TEXT}\nA closing line.\n`, `${SKILL_TEXT}\nA closing line, kept.\n`]) {
				const saved = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: docPath, text, thread_id: ownerId } });
				expect(saved.ok(), await saved.text()).toBe(true);
			}
			expect(fs.existsSync(docPath)).toBe(true);
		},
	});
	await expect(page.locator('.cm-scroller')).toBeVisible();
	await widen(page, doc);
	await expect(card(page, doc, FIRST)).toBeVisible();
	const history = page.getByRole('button', { name: 'History', exact: true });
	for (const close of ['the History button', 'the history drawer Close button'] as const) {
		await history.click();
		await expect(page.locator('[data-doc-history-drawer]')).toBeVisible();
		// While History is open the column is closed: that is unchanged.
		await expect(page.locator('[data-margin-card]')).toHaveCount(0);
		if (close === 'the History button') await history.click(); else await page.getByRole('button', { name: 'Close history', exact: true }).click();
		await expect(page.locator('[data-doc-history-drawer]'), close).toHaveCount(0);
		await expect(card(page, doc, FIRST), `cards are back after ${close}`).toBeVisible();
		await expect(page.locator('.canvas-comment-drawer.margin')).toBeVisible();
		expect(await page.locator('[data-margin-card]').count()).toBe(ITEMS.length);
	}
	// A reader who had the column closed before History opened does not get it opened after.
	await pill(page).click();
	await expect(page.locator('[data-margin-card]')).toHaveCount(0);
	await history.click();
	await expect(page.locator('[data-doc-history-drawer]')).toBeVisible();
	await history.click();
	await expect(page.locator('[data-doc-history-drawer]')).toHaveCount(0);
	await page.waitForTimeout(600);
	await expect(page.locator('[data-margin-card]')).toHaveCount(0);
});
