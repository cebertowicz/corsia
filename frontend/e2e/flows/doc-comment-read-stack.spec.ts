import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { openSuggestionDocument, type SeededReviewItem } from '../support/docSuggestions';

/**
 * A stack of comments on one text is nested highlights. A stack of read comments must paint as one wash, the same as a single
 * read comment: the outermost highlight draws it and the ones nested inside draw nothing of their own. A stack with an unread
 * comment keeps the unread wash and underline, which are stronger than a read one.
 */

const CLEAR = 'rgba(0, 0, 0, 0)';
const comments = (quote: string, count: number, author: 'agent' | 'person'): SeededReviewItem[] => Array.from({ length: count }, (_, i) => ({ quote, body: `Question ${i + 1} about ${quote}`, author }));

const SOLO = 'solo read phrase';
const READ7 = 'read seven phrase';
const READ3 = 'read three phrase';
const UNREAD7 = 'unread seven phrase';
const UNREAD1 = 'unread solo phrase';
const TEXT = [
	'# Read stacks', '',
	`The first sample is the ${SOLO} in this sentence, and it carries on for a while after that.`, '',
	`The second sample is the ${READ7} in this sentence, and it carries on for a while after that.`, '',
	`The third sample is the ${READ3} in this sentence, and it carries on for a while after that.`, '',
	`The fourth sample is the ${UNREAD7} in this sentence, and it carries on for a while after that.`, '',
	`The fifth sample is the ${UNREAD1} in this sentence, and it carries on for a while after that.`, '',
	'| Milestone | Owner |', '| --- | --- |', '| Design freeze | Priya Raman |', '| Beta | Sam Ortiz |',
].join('\n');

type Layer = { unread: boolean; bg: string; borderStyle: string; borderWidth: string; shadow: string };

/** The highlight spans around a phrase, outermost first, with what each paints; for a table cell, the cell itself. */
function layersAround(page: Page, phrase: string): Promise<Layer[]> {
	return page.evaluate((phrase) => {
		const content = document.querySelector('.cm-content')!;
		const walker = document.createTreeWalker(content, NodeFilter.SHOW_TEXT);
		let host: HTMLElement | null = null;
		for (let node = walker.nextNode(); node && !host; node = walker.nextNode()) if ((node.textContent ?? '').includes(phrase)) host = node.parentElement;
		const chain: HTMLElement[] = [];
		for (let el = host; el && el !== content; el = el.parentElement) if (el.classList.contains('cm-comment-anchor')) chain.push(el);
		return chain.reverse().map((el) => {
			const style = getComputedStyle(el);
			return { unread: el.hasAttribute('data-unread'), bg: style.backgroundColor, borderStyle: style.borderBottomStyle, borderWidth: style.borderBottomWidth, shadow: style.boxShadow };
		});
	}, phrase);
}

test('@flow:document-comments a stack of read comments paints as one wash, an unread stack keeps the stronger unread paint', async ({ page, request }) => {
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 1440, height: 1000 });
	// The canvas stamps a comment read as it is seen: keep the seeded ones unread for the check.
	await page.route('**/comments/*/viewed', (route) => route.abort());
	await openSuggestionDocument(page, request, TEXT, [
		...comments(SOLO, 1, 'person'), ...comments(READ7, 7, 'person'), ...comments(READ3, 3, 'person'),
		...comments(UNREAD7, 7, 'agent'), ...comments(UNREAD1, 1, 'agent'),
		...comments('Priya Raman', 3, 'person'), ...comments('Sam Ortiz', 1, 'person'),
	]);
	await expect.poll(() => page.locator('.cm-comment-anchor').count(), { timeout: 30_000 }).toBeGreaterThan(0);
	await expect.poll(() => layersAround(page, UNREAD7).then((layers) => layers.length), { timeout: 30_000, message: 'the 7-comment stacks are drawn as nested highlights' }).toBe(7);

	const solo = await layersAround(page, SOLO);
	expect(solo, 'a single read comment is one highlight').toHaveLength(1);
	expect(solo[0].unread).toBe(false);
	expect(solo[0].bg, 'a read highlight has a wash').not.toBe(CLEAR);
	expect(solo[0].borderStyle).toBe('solid');

	for (const [phrase, count] of [[READ7, 7], [READ3, 3]] as const) {
		const stack = await layersAround(page, phrase);
		expect(stack, `${phrase} is ${count} nested highlights`).toHaveLength(count);
		expect(stack[0].bg, `${phrase}: the outermost highlight paints the same wash as a single read comment`).toBe(solo[0].bg);
		expect(stack[0].borderStyle).toBe(solo[0].borderStyle);
		expect(stack[0].borderWidth).toBe(solo[0].borderWidth);
		for (const nested of stack.slice(1)) {
			expect(nested.bg, `${phrase}: a read highlight inside a read one draws no wash`).toBe(CLEAR);
			expect(nested.borderStyle, `${phrase}: and no rule`).toBe('none');
		}
	}

	const unread = await layersAround(page, UNREAD1);
	expect(unread, 'a single unread comment is one highlight').toHaveLength(1);
	expect(unread[0].unread).toBe(true);
	expect(unread[0].bg, 'unread paints a different wash from read').not.toBe(solo[0].bg);
	expect(unread[0].borderStyle).toBe('solid');
	expect(parseFloat(unread[0].borderWidth), 'the unread underline is thicker than the read one').toBeGreaterThan(parseFloat(solo[0].borderWidth));
	const unreadStack = await layersAround(page, UNREAD7);
	expect(unreadStack.every((layer) => layer.unread), 'every comment in the unread stack is drawn unread').toBe(true);
	expect(unreadStack[0].bg).not.toBe(solo[0].bg);
	expect(unreadStack[0].borderStyle).toBe('solid');

	// A table cell is one element however many comments it holds: three paint the same as one.
	const cell3 = await layersAround(page, 'Priya Raman');
	const cell1 = await layersAround(page, 'Sam Ortiz');
	expect(cell1, 'a commented cell is one highlight').toHaveLength(1);
	expect(cell3, 'a cell with three comments is still one highlight').toHaveLength(1);
	expect(cell3[0].bg).toBe(cell1[0].bg);
	expect(cell3[0].shadow).toBe(cell1[0].shadow);
});
