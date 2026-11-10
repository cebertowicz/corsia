import { expect, type Page, type APIRequestContext } from '@playwright/test';
import { openSuggestionDocument } from './docSuggestions';

/** The seeded document and helpers the margin-column specs share: six threads (a cluster of two on one line, two suggestions) beside ten paragraphs. */

export const para = (n: number) => `Paragraph ${n} of the plan says that agents hand work to each other, and every handoff should carry the intent behind it so the next owner never has to guess what was meant by the person who asked.`;
export const FIVE = 'Paragraph 5 notes alpha alphax and bravo bravox so that two notes sit on one line, then it carries on for a while so that the paragraph wraps onto a few lines of the page.';
export const TEXT = `# Handoff plan\n\n${[1, 2, 3, 4, 5, 6, 7, 8, 9, 10].map((i) => (i === 5 ? FIVE : para(i))).join('\n\n')}\n`;
// A comment, two comments on one line (the cluster), a suggestion, a suggestion, a comment, in document order.
export const ITEMS = [
	{ quote: 'Paragraph 1 of the plan', body: 'Which agents? Name them here.' },
	{ quote: 'alpha alphax', body: 'Alpha needs a source.' },
	{ quote: 'bravo bravox', body: 'Same for bravo.' },
	{ quote: 'Paragraph 10 of the plan', body: 'This contradicts the intro. Pick one.' },
	{ quote: 'Paragraph 3 of the plan', replacement: 'Paragraph 3 of the rollout plan', body: 'Clearer.' },
	{ quote: 'Paragraph 8 of the plan', replacement: 'Paragraph 8 of the intro', body: 'Match the intro.' },
];
export const [FIRST, ALPHA, BRAVO, LAST, SUGGESTION, SUGGESTION2] = [0, 1, 2, 3, 4, 5];

export type Doc = Awaited<ReturnType<typeof openSuggestionDocument>>;
export const pill = (page: Page) => page.getByRole('button', { name: /^\d+ comments?/i }).first();
export const card = (page: Page, doc: Doc, i: number) => page.locator(`[data-margin-card="${doc.ids[i]}"]`);
export const docWidth = (page: Page) => page.evaluate(() => Math.round(document.querySelector('.doc')?.getBoundingClientRect().width ?? 0));

/** Folds the sidebar and the Workspace panel away until the canvas has room for the column, then shows the column. */
export async function widen(page: Page, doc: Doc) {
	for (const name of ['Toggle sidebar', 'Toggle panel']) {
		if ((await docWidth(page)) >= 800) break;
		await page.getByRole('button', { name, exact: true }).click();
		await expect.poll(() => docWidth(page)).toBeGreaterThan(600);
	}
	if (!(await page.locator('[data-margin-card]').count())) await pill(page).click();
	await expect(page.locator('.canvas-comment-drawer.margin .margin-card').first()).toBeVisible();
	return doc;
}

/** Seeds the document and, on a desktop, widens the canvas so it has room for the column. */
export async function open(page: Page, request: APIRequestContext, { wide = true }: { wide?: boolean } = {}) {
	const doc = await openSuggestionDocument(page, request, TEXT, ITEMS, { splitWidth: wide ? 5000 : 540 });
	await expect(page.locator('.cm-scroller')).toBeVisible();
	if (wide) {
		await widen(page, doc);
		await expect(card(page, doc, FIRST)).toBeVisible();
	}
	return doc;
}

/** Top of the first drawn row of a thread's highlight (comment) or struck text (suggestion), and of its card. */
export const geometry = (page: Page, ids: string[]) => page.evaluate((all) => all.map((id) => {
	const wrap = document.querySelector<HTMLElement>(`[data-margin-card="${id}"]`);
	const mark = [...document.querySelectorAll<HTMLElement>('.cm-comment-line, .cm-suggestion-del, .cm-suggestion-insert')]
		.find((node) => (node.dataset.commentIds ?? node.dataset.suggestionId ?? '').split(',').includes(id));
	const c = wrap?.getBoundingClientRect(), m = mark?.getBoundingClientRect();
	return { id, card: c ? { top: c.top, bottom: c.bottom, left: c.left } : null, line: m ? m.top : null };
}), ids);
