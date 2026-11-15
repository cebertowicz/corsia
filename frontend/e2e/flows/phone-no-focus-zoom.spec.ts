import fs from 'node:fs';
import path from 'node:path';
import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { cell, openTableDocument, settled } from '../support/tableCells';
import { openDocument, openImage, seedImageDocument } from '../support/docImageComments';

/**
 * A phone's browser zooms the page in when a field under the input size (16px) takes focus, and the person has to pinch
 * back out to see the right edge. It also shrinks the page to fit anything wider than the screen. At 393 every artifact
 * view keeps the page at its own width, and every place a person types (the document's text, a table cell, a code file,
 * a comment's reply field) is at least the input size when it takes focus.
 *
 * Emulated Chromium never zooms on focus, so the font size is what is asserted; the page scale is asserted as well.
 */

const PHONE = { width: 393, height: 852 };
const INPUT_SIZE = 16;
const TABLE = [
	'| Column one | Column two | Column three | Column four | Column five | Column six | Column seven |',
	'| --- | --- | --- | --- | --- | --- | --- |',
	'| aaaaaaaaaaaa | trailhead sign points the wrong way | cccccccccccc | dddddddddddd | eeeeeeeeeeee | ffffffffffff | gggggggggggg |',
].join('\n');
const DOC = [
	'# A document read on a phone', '',
	'An ordinary paragraph of text that wraps on a phone the way any paragraph does.', '',
	TABLE, '',
	'```', 'const aVeryLongLineOfCode = someFunctionCall(withArguments, andMoreArguments, andEvenMoreArgumentsThatRunPastTheEdge);', '```', '',
	'Inline `code span` in a line.', '',
	'A bare link https://example.com/a/very/long/path/that/does/not/break/anywhere/at/all/because/it/has/no/spaces/in/it', '',
	'Closing paragraph.',
].join('\n');
const CODE = 'def a_function_with_a_long_signature(first_argument, second_argument, third_argument, fourth_argument):\n    return first_argument\n';

/** The focused element and its font size, every visible editable under the input size, and the page's scale. */
const typing = (page: Page) => page.evaluate((min) => {
	const name = (el: Element) => `${el.tagName.toLowerCase()}.${[...el.classList].slice(0, 3).join('.')}`;
	const active = document.activeElement as HTMLElement | null;
	const small = [...document.querySelectorAll<HTMLElement>('input, textarea, select, [contenteditable]')]
		.filter((el) => el.getAttribute('contenteditable') !== 'false' && el.getClientRects().length > 0 && !(el as HTMLInputElement).disabled
			&& !['hidden', 'file', 'checkbox', 'radio', 'range'].includes((el as HTMLInputElement).type))
		.filter((el) => parseFloat(getComputedStyle(el).fontSize) < min)
		.map((el) => `${name(el)} ${getComputedStyle(el).fontSize}`);
	return {
		focused: active && active !== document.body ? name(active) : null,
		focusedSize: active ? parseFloat(getComputedStyle(active).fontSize) : 0,
		small,
		scale: window.visualViewport?.scale ?? 1,
	};
}, INPUT_SIZE);

/** Elements whose box reaches past either side of the screen with no ancestor clipping them, and the page's own width. */
const overflow = (page: Page) => page.evaluate(() => {
	const vw = window.innerWidth;
	const name = (el: Element) => `${el.tagName.toLowerCase()}.${[...el.classList].slice(0, 3).join('.')}`;
	// Only an ancestor that is (or holds) a box's containing block clips it: a fixed or absolute box escapes the rest.
	const escapes = (el: Element) => {
		let pos = getComputedStyle(el).position;
		for (let p = el.parentElement; p && p !== document.documentElement; p = p.parentElement) {
			const s = getComputedStyle(p);
			const holds = pos === 'fixed'
				? s.transform !== 'none' || s.filter !== 'none' || /paint|layout|strict|content/.test(s.contain) || s.willChange.includes('transform')
				: pos === 'absolute' ? s.position !== 'static' || s.transform !== 'none' : true;
			if (holds && s.overflowX !== 'visible') return false;
			if (holds) pos = s.position;
		}
		return true;
	};
	const escaping = [...document.querySelectorAll<HTMLElement>('body *')]
		.filter((el) => {
			const r = el.getBoundingClientRect();
			return r.width > 0 && r.height > 0 && (r.right > vw + 0.5 || r.left < -0.5) && getComputedStyle(el).visibility !== 'hidden' && escapes(el);
		})
		.slice(0, 10)
		.map((el) => `${name(el)} ${Math.round(el.getBoundingClientRect().left)}..${Math.round(el.getBoundingClientRect().right)}`);
	return { escaping, pageWidth: document.documentElement.scrollWidth, vw };
});

async function expectNoZoomOnFocus(page: Page, where: string) {
	await expect.poll(async () => (await typing(page)).focused, `${where}: something takes focus`).not.toBeNull();
	const now = await typing(page);
	expect(now.focusedSize, `${where}: ${now.focused} takes focus at ${now.focusedSize}px`).toBeGreaterThanOrEqual(INPUT_SIZE);
	expect(now.small, `${where}: editables under ${INPUT_SIZE}px`).toEqual([]);
	expect(now.scale, `${where}: the page stays at scale 1`).toBe(1);
}

async function expectFitsScreen(page: Page, where: string) {
	const now = await overflow(page);
	expect(now.escaping, `${where}: elements past the screen's edge`).toEqual([]);
	expect(now.pageWidth, `${where}: the page is no wider than the screen`).toBeLessThanOrEqual(now.vw);
}

test('@flow:phone-no-focus-zoom a document with a wide table and code fits the screen, and typing in it does not zoom', async ({ page }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-mobile', 'phone width runs in the touch project');
	test.setTimeout(120_000);
	await openTableDocument(page, PHONE, { text: DOC, quoted: 'ordinary paragraph' });
	await expectFitsScreen(page, 'document open');

	// The person taps into the text to type.
	await page.locator('.cm-line', { hasText: 'Closing paragraph' }).first().tap();
	await expectNoZoomOnFocus(page, 'document text');
	await expectFitsScreen(page, 'document text');

	// They tap a table cell to change it: its editor opens in the cell.
	await cell(page, 1, 0).tap();
	await expect(page.locator('.cm-md-cell-editor .cm-content')).toBeFocused();
	await expectNoZoomOnFocus(page, 'table cell');
	await expectFitsScreen(page, 'table cell');
	await page.keyboard.press('Escape');

	// They open the comments and tap into a reply.
	await page.locator('.comment-status').tap();
	const card = page.locator('[data-comment-id]').first();
	await expect(card).toBeVisible();
	await settled(page);
	await card.locator('.ct').tap();
	const reply = card.getByPlaceholder('Reply…');
	await expect(reply).toBeVisible();
	await reply.tap();
	await expect(reply).toBeFocused();
	await expectNoZoomOnFocus(page, 'comment reply');
	await expectFitsScreen(page, 'comment reply');
});

test('@flow:phone-no-focus-zoom a code file fits the screen, and typing in it does not zoom', async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-mobile', 'phone width runs in the touch project');
	test.setTimeout(120_000);
	await page.setViewportSize(PHONE);
	const title = `phone-zoom-code-${Date.now()}`;
	const threadId = await createSession(request, title);
	const params = { token: TEST_TOKEN };
	const roots: Array<{ kind: string; path: string }> = (await (await request.get('/api/files/tree', { params })).json()).roots ?? [];
	const filePath = path.join((roots.find((root) => root.kind === 'workspace') ?? roots[0]).path, `${title}.py`);
	fs.writeFileSync(filePath, CODE);
	const registered = await request.post('/api/files/register', { params, data: { path: filePath, thread_id: threadId } });
	expect(registered.ok(), await registered.text()).toBeTruthy();
	const artifactId = (await registered.json()).artifact.id as string;
	await page.addInitScript(({ threadId, artifactId, filePath, title }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: { tabs: [{ instanceId: 'code-tab', artifactId, path: filePath, title, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'code-tab', visible: true, activeSurface: 'artifact' } }, splitWidth: null,
		}));
	}, { threadId, artifactId, filePath, title: `${title}.py` });
	await loginByUrl(page, { waitForComposer: false });
	await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.locator('.cm-content')).toContainText('a_function_with_a_long_signature', { timeout: 20_000 });
	await settled(page);
	await expectFitsScreen(page, 'code open');

	await page.locator('.cm-line').first().tap();
	await expectNoZoomOnFocus(page, 'code text');
	await expectFitsScreen(page, 'code text');
});

test('@flow:phone-no-focus-zoom a document with an image, and the image opened, fit the screen', async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-mobile', 'phone width runs in the touch project');
	test.setTimeout(120_000);
	await page.setViewportSize(PHONE);
	const seeded = await seedImageDocument(page, request, `phonezoomimg${Date.now()}`, null);
	await openDocument(page, seeded, PHONE.width);
	await expectFitsScreen(page, 'image document');
	await openImage(page);
	await expectFitsScreen(page, 'image viewer');
	expect((await typing(page)).scale, 'image viewer: the page stays at scale 1').toBe(1);
});
