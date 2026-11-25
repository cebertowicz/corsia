import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { openSuggestionDocument, setEvidenceViewport } from '../support/docSuggestions';
import { enterDocumentEdit } from '../support/documentReading';
import { inEditorView } from '../support/liveDocument';
import { settled } from '../support/tableCells';

const DOC = [
	'# Suggestions', '',
	'A plain paragraph with paraword inside it.', '',
	'A sentence with suggestedword in it.', '',
	...Array.from({ length: 10 }, (_, i) => `Filler ${i}: words so the document scrolls.\n`),
].join('\n');

async function openDocument(page: Page, request: Parameters<typeof openSuggestionDocument>[1], info: Parameters<typeof setEvidenceViewport>[1]) {
	await setEvidenceViewport(page, info);
	await openSuggestionDocument(page, request, DOC, [{ quote: 'suggestedword', replacement: 'proposedword', body: 'A suggestion on this text.' }], { splitWidth: 900 });
	await settled(page);
}

/** The middle of a word in the open document, once the page has stopped moving. */
async function wordCentre(page: Page, needle: string) {
	const find = () => page.evaluate((word) => {
		const walker = document.createTreeWalker(document.querySelector('.doc .cm-content')!, NodeFilter.SHOW_TEXT);
		for (let n = walker.nextNode(); n; n = walker.nextNode()) {
			const at = (n.textContent ?? '').indexOf(word);
			if (at < 0) continue;
			(n.parentElement as HTMLElement).scrollIntoView({ block: 'center' });
			const range = document.createRange();
			range.setStart(n, at); range.setEnd(n, at + word.length);
			const box = range.getBoundingClientRect();
			return { x: box.left + box.width / 2, y: box.top + box.height / 2 };
		}
		throw new Error(`no ${word}`);
	}, needle);
	await find();
	await page.waitForTimeout(500);
	return find();
}

test.describe('@flow:doc-suggestion-selection @surface:documents', () => {
	// A long press on text under a suggestion starts a native selection; the browser's pointer-over for that press must not
	// redraw the suggestion's marks, which replaces the nodes the selection sits in and drops it (and Comment with it).
	for (const mode of ['Reading', 'Edit'] as const) {
		for (const way of ['long-press', 'long-press and handle'] as const) {
			test(`${mode} ${way} on suggested text keeps the selection and offers Comment at 393`, async ({ page, request }, info) => {
				test.skip(info.project.name !== 'chromium-mobile', 'touch');
				await openDocument(page, request, info);
				if (mode === 'Edit') { await enterDocumentEdit(page); await settled(page); }
				const spot = await wordCentre(page, 'suggestedword');
				const cdp = await page.context().newCDPSession(page);
				await cdp.send('Input.synthesizeTapGesture', { x: spot.x, y: spot.y, duration: 1200, tapCount: 1, gestureSourceType: 'touch' });
				if (way === 'long-press and handle') {
					await page.waitForTimeout(900);
					// What dragging the end handle does to the DOM selection: the far end moves to the end of the text.
					await page.evaluate(() => {
						const selection = document.getSelection()!;
						const node = selection.focusNode;
						// No selection left: the assertion below reports it, not a thrown range error.
						if (!node || node.nodeType !== Node.TEXT_NODE || selection.isCollapsed) return;
						selection.extend(node, (node.textContent ?? '').length);
					});
				}
				await expect.poll(() => page.evaluate(() => document.getSelection()?.toString() ?? ''), { timeout: 5_000 }).toContain('suggestedword');
				await expect(page.locator('.sel-comment').first()).toBeVisible();
			});
		}
	}

	// A mouse hovering a suggestion still highlights it, with and without text selected elsewhere.
	for (const selected of [false, true]) {
		test(`hovering a suggestion highlights it ${selected ? 'with' : 'without'} text selected elsewhere`, async ({ page, request }, info) => {
			test.skip(info.project.name !== 'chromium-desktop', 'mouse');
			await openDocument(page, request, info);
			if (selected) {
				await inEditorView(page, (view) => {
					const at = view.state.doc.toString().indexOf('paraword');
					view.dispatch({ selection: { anchor: at, head: at + 8 } });
				}, null);
			}
			const spot = await wordCentre(page, 'suggestedword');
			await page.mouse.move(spot.x, spot.y);
			await expect(page.locator('.doc .cm-suggestion-focus').first()).toBeVisible();
			await page.mouse.move(5, 5);
			await expect(page.locator('.doc .cm-suggestion-focus')).toHaveCount(0);
		});
	}
});
