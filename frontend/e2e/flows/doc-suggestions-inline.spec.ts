import { showAllThreads } from '../support/commentDrawer';
import { test } from '../support/test';
import { expect, type Locator, type Page } from '@playwright/test';
import { saveEvidenceScreenshot } from '../support/corsia';
import { openSuggestionDocument } from '../support/docSuggestions';
import { layouts } from '../support/liveConcurrency';

// The editor's inline marks for one suggestion: the struck quote and the drawn insert.
const strike = (page: Page, id: string) => page.locator(`.cm-content .cm-suggestion-del[data-suggestion-id="${id}"]`);
const insert = (page: Page, id: string) => page.locator(`.cm-content .cm-suggestion-insert[data-suggestion-id="${id}"]`);
const commentsButton = (page: Page) => page.getByRole('button', { name: /^\d+ open threads?\b/ });

async function openComments(page: Page, drawer: Locator) {
	await commentsButton(page).click();
	await expect(drawer).toBeVisible();
	await showAllThreads(drawer);
}

/** A colour as the browser computes it, so a hex value and an rgb() value compare. */
async function computedColour(page: Page, value: string): Promise<string> {
	return page.evaluate((colour) => {
		const probe = document.createElement('span');
		probe.style.color = colour;
		document.body.append(probe);
		const resolved = getComputedStyle(probe).color;
		probe.remove();
		return resolved;
	}, value);
}

/** Neither the element nor anything inside it is wider than the box it scrolls in. */
async function expectNoSidewaysScroll(target: Locator, label: string) {
	const { scrollWidth, clientWidth } = await target.evaluate((element) => ({ scrollWidth: element.scrollWidth, clientWidth: element.clientWidth }));
	expect(scrollWidth, `${label} has no sideways scroll`).toBeLessThanOrEqual(clientWidth);
}

const PROSE = '# Inline\n\nThe first line is loose.\n\nThe second line is fine.\n';

for (const [width, height, project] of layouts) {
	test.describe(`inline suggestions at ${width}`, () => {
		test.beforeEach(async ({ page }, testInfo) => {
			test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
			test.setTimeout(90_000);
			await page.setViewportSize({ width, height });
		});

		test(`@flow:document-suggestions an open suggestion shows inline in its author's colour and changes nothing until accepted at ${width}`, async ({ page, request }, testInfo) => {
			const { drawer, editor, ids: [loose], row, storedText } = await openSuggestionDocument(page, request, PROSE, [
				{ quote: 'is loose', replacement: 'is tight', body: 'Tighten the first line.' },
			]);
			await expect(strike(page, loose)).toHaveText('is loose');
			await expect(strike(page, loose)).toHaveCSS('text-decoration-line', /line-through/);
			await expect(insert(page, loose)).toHaveAttribute('data-insert', 'is tight');
			expect(await insert(page, loose).evaluate((element) => getComputedStyle(element, '::after').content)).toBe('"is tight"');
			// The suggestion's anchor is its marks, not the amber comment highlight.
			await expect(page.locator('.cm-comment-anchor')).toHaveCount(0);

			// The insert is drawn, not typed: the document text and the saved file still hold the quote.
			await expect(editor).toContainText('The first line is loose.');
			await expect(editor).not.toContainText('is tight');
			expect(await storedText()).toBe(PROSE);

			// The author's colour: the card's avatar and the inline marks share it, and the marks' text takes its tone for the theme.
			await openComments(page, drawer);
			const avatar = await row(loose).locator('.avatar').evaluate((element) => getComputedStyle(element).backgroundColor);
			const marks = await insert(page, loose).evaluate((element) => {
				const style = getComputedStyle(element);
				const light = document.documentElement.dataset.theme === 'light';
				return {
					colour: style.getPropertyValue('--suggestion-color').trim(),
					tone: style.getPropertyValue(light ? '--suggestion-tone-light' : '--suggestion-tone-dark').trim(),
					text: style.color,
				};
			});
			expect(await computedColour(page, marks.colour)).toBe(avatar);
			expect(marks.text).toBe(await computedColour(page, marks.tone));
			await expect(strike(page, loose)).toHaveCSS('color', marks.text);
			await saveEvidenceScreenshot(page, testInfo, `document-suggestions-inline-open-${width}`, { fullPage: false });
		});

		test(`@flow:document-suggestions pressing an inline suggestion focuses its card once the comments list is closed at ${width}`, async ({ page, request }) => {
			const { drawer, editor, ids: [loose, fine] } = await openSuggestionDocument(page, request, PROSE, [
				{ quote: 'is loose', replacement: 'is tight', body: 'Tighten the first line.' },
				{ quote: 'is fine', replacement: 'is wordy', body: 'Loosen the second line.' },
			]);
			// With no list open the cards show in the popover (or phone drawer) grouped as "Open comments", which
			// carries no `data-comment-id`, so each card is found by its role and its own body text.
			const card = (body: string) => page.getByRole('region', { name: 'Comment threads', exact: true })
				.getByRole('region', { name: /^Comment by / }).filter({ hasText: body });
			// A phone user taps; the desktop user clicks.
			if (project === 'chromium-mobile') await insert(page, loose).tap(); else await insert(page, loose).click();
			await expect(card('Tighten the first line.')).toBeVisible();
			await expect(card('Tighten the first line.')).toHaveClass(/\bfocused\b/);
			// On a phone the card opens in a bottom sheet: a tap on the insert must leave its decision within reach.
			if (project === 'chromium-mobile') {
				await expect(card('Tighten the first line.').getByRole('button', { name: 'Accept', exact: true })).toBeInViewport();
				await expect(card('Tighten the first line.').getByRole('button', { name: 'Reject', exact: true })).toBeInViewport();
			}
			// The popover holds that one thread, not the other suggestion's.
			await expect(card('Loosen the second line.')).toHaveCount(0);
			// Pressing the insert never edits the document.
			await expect(editor).toContainText('The first line is loose.');
			await page.keyboard.press('Escape');
			await expect(card('Tighten the first line.')).toHaveCount(0);

			await openComments(page, drawer);
			// The list is a modal sheet over the document column (its scrim and grabber included) in both layouts here, since
			// neither canvas reaches 900 px (RSR-7): an insert under it cannot be pressed until the list closes.
			const covered = await insert(page, fine).evaluate((element) => {
				const rect = element.getBoundingClientRect();
				return !!document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2)?.closest('[data-adaptive-drawer]');
			});
			expect(covered, 'the insert is under the list sheet and its scrim').toBe(true);
			await page.getByRole('button', { name: 'Close comments', exact: true }).click();
			await expect(drawer).toHaveCount(0);
			if (project === 'chromium-mobile') await insert(page, fine).tap(); else await insert(page, fine).click();
			await expect(card('Loosen the second line.')).toBeVisible();
			await expect(card('Loosen the second line.')).toHaveClass(/\bfocused\b/);
			await expect(card('Tighten the first line.')).toHaveCount(0);
		});

		test(`@flow:document-suggestions accepting applies the change and rejecting leaves the file, and both clear the inline marks at ${width}`, async ({ page, request }) => {
			const { accept, drawer, editor, ids: [loose, fine], reject, row, storedText } = await openSuggestionDocument(page, request, PROSE, [
				{ quote: 'is loose', replacement: 'is tight', body: 'Tighten the first line.' },
				{ quote: 'is fine', replacement: 'is wordy', body: 'Loosen the second line.' },
			]);
			await expect(insert(page, loose)).toHaveCount(1);
			await expect(insert(page, fine)).toHaveCount(1);
			await openComments(page, drawer);

			await accept(loose).click();
			await expect(row(loose).getByRole('region', { name: /^accepted suggestion by / })).toBeVisible();
			await expect(editor).toContainText('The first line is tight.');
			await expect(strike(page, loose)).toHaveCount(0);
			await expect(insert(page, loose)).toHaveCount(0);
			await expect.poll(storedText).toBe(PROSE.replace('is loose', 'is tight'));

			await reject(fine).click();
			await expect(row(fine).getByRole('region', { name: /^rejected suggestion by / })).toBeVisible();
			await expect(strike(page, fine)).toHaveCount(0);
			await expect(insert(page, fine)).toHaveCount(0);
			await expect(editor).toContainText('The second line is fine.');
			expect(await storedText()).toBe(PROSE.replace('is loose', 'is tight'));
		});

		test(`@flow:document-suggestions of two overlapping suggestions the earlier is inline and the later is card-only at ${width}`, async ({ page, request }) => {
			const text = '# Overlap\n\nThe quick brown fox jumps.\n';
			const { accept, drawer, ids: [earlier, later], row } = await openSuggestionDocument(page, request, text, [
				{ quote: 'quick brown', replacement: 'slow red', body: 'Slow it down.' },
				{ quote: 'brown fox', replacement: 'grey wolf', body: 'A different animal.' },
			]);
			await expect(strike(page, earlier)).toHaveText('quick brown');
			await expect(insert(page, earlier)).toHaveAttribute('data-insert', 'slow red');
			await expect(strike(page, later)).toHaveCount(0);
			await expect(insert(page, later)).toHaveCount(0);
			// One strike and one insert in the whole editor: the later suggestion draws nothing over the earlier.
			await expect(page.locator('.cm-content .cm-suggestion-del')).toHaveCount(1);
			await expect(page.locator('.cm-content .cm-suggestion-insert')).toHaveCount(1);
			await openComments(page, drawer);
			await expect(row(earlier)).toContainText('Slow it down.');
			await expect(row(later)).toContainText('A different animal.');
			await expect(accept(later)).toBeEnabled();
		});

		test(`@flow:document-suggestions typing inside a quote drops that suggestion's inline marks and keeps its card at ${width}`, async ({ page, request }) => {
			// Loud quarantine of a product defect (EAS anti-pattern #10): tracked in #1009, owned by Documents & Artifacts; remove this line when it is fixed, and review it by 2026-10-24 whether or not it is.
			test.fixme(true, 'cebertowicz/corsia#1009 (review by 2026-10-24): a caret reaching a suggestion quote reopens its card on every caret move, so the editor cannot take the typed character on a canvas under 900 px');
			const { drawer, editor, ids: [loose, fine], row } = await openSuggestionDocument(page, request, PROSE, [
				{ quote: 'is loose', replacement: 'is tight', body: 'Tighten the first line.' },
				{ quote: 'is fine', replacement: 'is wordy', body: 'Loosen the second line.' },
			]);
			// A caret landing in commented text must leave the editor focused to take the edit; pending #1009 it reopens the card.
			await openComments(page, drawer);
			await expect(strike(page, loose)).toHaveText('is loose');
			// The middle of the struck quote, not either edge, so the typed character lands inside it.
			await strike(page, loose).click();
			await page.keyboard.type('x');
			await expect(editor).not.toContainText('The first line is loose.');
			await expect(strike(page, loose)).toHaveCount(0);
			await expect(insert(page, loose)).toHaveCount(0);
			// Only that suggestion: the other keeps its marks.
			await expect(strike(page, fine)).toHaveText('is fine');
			await expect(insert(page, fine)).toHaveAttribute('data-insert', 'is wordy');
			await expect(row(loose)).toContainText('Tighten the first line.');
		});

		test(`@flow:document-suggestions deleting the first character of a quote drops that suggestion's inline marks and keeps its card at ${width}`, async ({ page, request }) => {
			const { drawer, editor, ids: [loose, fine], row } = await openSuggestionDocument(page, request, PROSE, [
				{ quote: 'is loose', replacement: 'is tight', body: 'Tighten the first line.' },
				{ quote: 'is fine', replacement: 'is wordy', body: 'Loosen the second line.' },
			]);
			// This canvas is narrower than 900 px, so the list is a modal sheet over the document column (RSR-7): the sheet is
			// closed before the document is touched, and the card is still listed when it opens again.
			await openComments(page, drawer);
			await expect(strike(page, loose)).toHaveText('is loose');
			await page.getByRole('button', { name: 'Close comments', exact: true }).click();
			await expect(drawer).toHaveCount(0);
			// A caret reaching the quote opens its card as a modal sheet too, and every caret move inside the quote opens it again,
			// so the card closes first and the editor, which keeps its caret at the quote's start, takes the edit: deleting the
			// quote's first character.
			// The click lands at the line's end, clear of the margin markers on its left, and Home returns the caret to its start.
			const line = editor.locator('.cm-line', { hasText: 'The first line is loose.' });
			await line.click({ position: { x: (await line.boundingBox())!.width - 4, y: 5 } });
			await page.keyboard.press('Home');
			const card = page.getByRole('dialog');
			for (let step = 0; step < 18 && !(await card.isVisible()); step += 1) await page.keyboard.press('ArrowRight');
			await expect(card).toBeVisible();
			await page.keyboard.press('Escape');
			await expect(card).toHaveCount(0);
			await expect(editor).toBeFocused();
			await page.keyboard.press('Delete');
			await expect(editor).not.toContainText('The first line is loose.');
			await expect(editor).toContainText('The first line s loose.');
			await expect(strike(page, loose)).toHaveCount(0);
			await expect(insert(page, loose)).toHaveCount(0);
			// Only that suggestion: the other keeps its marks.
			await expect(strike(page, fine)).toHaveText('is fine');
			await expect(insert(page, fine)).toHaveAttribute('data-insert', 'is wordy');
			// The edit moved the caret within the quote, which opens a sheet again on a phone: close it, then list the comments.
			await page.keyboard.press('Escape');
			await expect(page.getByRole('dialog')).toHaveCount(0);
			await openComments(page, drawer);
			await expect(row(loose)).toContainText('Tighten the first line.');
		});

		test(`@flow:document-suggestions long inserts wrap at word boundaries with no sideways scroll, inline and on the card, at ${width}`, async ({ page, request }, testInfo) => {
			const text = '# Wrap\n\nA short line.\n\nA brief line.\n';
			const unbroken = `https://example.com/${'segment'.repeat(40)}`;
			const words = Array.from({ length: 48 }, (_, index) => `word${index}`).join(' ');
			const { drawer, ids: [token, prose], row } = await openSuggestionDocument(page, request, text, [
				{ quote: 'short', replacement: unbroken, body: 'One long link.' },
				{ quote: 'brief', replacement: words, body: 'Many words.' },
			]);
			await expect(insert(page, token)).toHaveAttribute('data-insert', unbroken);
			await expect(insert(page, prose)).toHaveAttribute('data-insert', words);
			await expectNoSidewaysScroll(page.locator('.cm-scroller'), 'the editor');
			for (const [id, label] of [[token, 'the unbroken insert'], [prose, 'the multi-word insert']] as const) {
				const shape = await insert(page, id).evaluate((element) => {
					const style = getComputedStyle(element);
					return { lines: new Set([...element.getClientRects()].map((rect) => Math.round(rect.top))).size, wordBreak: style.wordBreak, overflowWrap: style.overflowWrap };
				});
				expect(shape.lines, `${label} wraps onto more than one line`).toBeGreaterThan(1);
				// Words break only between words; one token with no break opportunity falls back to anywhere.
				expect(shape.wordBreak, `${label} breaks between words`).toBe('normal');
				expect(shape.overflowWrap, `${label} wraps an unbreakable token`).toBe('anywhere');
			}
			await saveEvidenceScreenshot(page, testInfo, `document-suggestions-inline-wrap-${width}`, { fullPage: false });

			await openComments(page, drawer);
			await expectNoSidewaysScroll(drawer.locator('.drawer-comments'), 'the comments list');
			for (const [id, label] of [[token, 'the unbroken card'], [prose, 'the multi-word card']] as const) {
				await row(id).scrollIntoViewIfNeeded();
				await expectNoSidewaysScroll(row(id), label);
				await expectNoSidewaysScroll(row(id).locator('.ct'), `${label} body`);
				await expectNoSidewaysScroll(row(id).locator('.diff-body'), `${label} diff`);
			}
			// On the card the insert is real text, so each word can be measured: none is split across lines.
			const split = await row(prose).locator('.diff-line.add .diff-text').evaluate((element) => {
				const node = document.createTreeWalker(element, NodeFilter.SHOW_TEXT).nextNode()!;
				const content = node.textContent ?? '';
				const tops = new Set<number>();
				const broken: string[] = [];
				for (const match of content.matchAll(/\S+/g)) {
					const range = document.createRange();
					range.setStart(node, match.index!);
					range.setEnd(node, match.index! + match[0].length);
					const lines = new Set([...range.getClientRects()].filter((rect) => rect.width > 0).map((rect) => Math.round(rect.top)));
					lines.forEach((top) => tops.add(top));
					if (lines.size > 1) broken.push(match[0]);
				}
				return { broken, lines: tops.size };
			});
			expect(split.lines, 'the multi-word card wraps onto more than one line').toBeGreaterThan(1);
			expect(split.broken, 'no word on the multi-word card is split across lines').toEqual([]);
			await saveEvidenceScreenshot(page, testInfo, `document-suggestions-card-wrap-${width}`, { fullPage: false });
		});
	});
}
