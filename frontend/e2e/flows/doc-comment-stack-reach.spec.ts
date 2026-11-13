import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { openSuggestionDocument, type SeededReviewItem } from '../support/docSuggestions';
import { closeComments, openComments } from '../support/commentDrawer';

/**
 * A comment is never out of reach. A stack of comments on one text (prose or a table cell) must have every comment reachable
 * from the document at 1440 and on a phone: through the card a press opens, or the list of the document's comments the header
 * opens. The unread paint of a stack (`doc-comment-read-stack.spec.ts`) only colours it, so this holds whatever it draws.
 */

const PROSE = 'stacked prose phrase';
const CELL = 'Priya Raman';
const TEXT = ['# Stacks', '', `A sentence that carries the ${PROSE} and then goes on for a while after it.`, '', '| Milestone | Owner |', '| --- | --- |', `| Design freeze | ${CELL} |`].join('\n');
const bodies = (label: string) => [1, 2, 3].map((n) => `${label} note ${n}`);
const items = (quote: string, label: string, author: 'agent' | 'person'): SeededReviewItem[] => bodies(label).map((body) => ({ quote, body, author }));

/** The text of everything a card shows: the popover on a desktop, the sheet on a phone. */
const cardText = (page: Page, phone: boolean) => (phone ? page.locator('[data-adaptive-drawer] .drawer-panel') : page.locator('[data-popover]')).allInnerTexts().then((parts) => parts.join('\n'));

for (const author of ['person', 'agent'] as const) {
	test(`@flow:document-comments every comment of a ${author === 'person' ? 'read' : 'unread'} stack in prose and in a table cell is reachable`, async ({ page, request }, info) => {
		test.setTimeout(150_000);
		const phone = info.project.name.includes('mobile');
		await page.setViewportSize(phone ? { width: 393, height: 852 } : { width: 1440, height: 1000 });
		// The canvas stamps a comment read as it is seen: keep the agent's ones unread.
		await page.route('**/comments/*/viewed', (route) => route.abort());
		await openSuggestionDocument(page, request, TEXT, [...items(PROSE, 'Prose', author), ...items(CELL, 'Cell', author)]);
		await expect.poll(() => page.locator('.cm-comment-anchor').count(), { timeout: 30_000 }).toBeGreaterThan(0);

		for (const [label, target] of [['Prose', page.locator('.cm-line .cm-comment-anchor', { hasText: PROSE }).first()], ['Cell', page.locator('td .cm-comment-anchor', { hasText: CELL }).first()]] as const) {
			await expect(target).toBeVisible();
			if (phone) await target.tap(); else await target.click();
			await expect.poll(() => cardText(page, phone).then((text) => text.length), { message: `a press on the ${label} stack opens a card` }).toBeGreaterThan(0);
			await page.waitForTimeout(500);
			const pressed = await cardText(page, phone);
			const reachedByPress = bodies(label).filter((body) => pressed.includes(body));
			await page.keyboard.press('Escape');
			await expect.poll(() => cardText(page, phone).then((text) => text.length), { message: 'Escape closes the card' }).toBe(0);
			await openComments(page);
			const drawer = page.getByRole('region', { name: 'Document comments', exact: true });
			for (const body of bodies(label)) {
				const inPress = reachedByPress.includes(body);
				const inList = (await drawer.innerText()).includes(body);
				expect(inPress || inList, `${label} stack, ${author}: "${body}" is reachable by the press or the document's list`).toBe(true);
			}
			console.log(`STACKREACH ${author} ${phone ? 393 : 1440} ${label} press reached ${reachedByPress.length} of 3`);
			await closeComments(page);
			await expect(page.getByRole('region', { name: 'Document comments', exact: true })).toHaveCount(0);
		}
	});
}
