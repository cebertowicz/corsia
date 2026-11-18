import fs from 'node:fs';
import path from 'node:path';
import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { TEST_TOKEN } from '../support/corsia';
import { syntheticPng } from '../support/syntheticImages';
import { openLiveDocument } from '../support/liveDocument';
import { agentComment, reviewerSession } from '../support/liveConcurrency';
import { agentCreatesSharedDocument, demoteAgent, openAgentBeside, SHARED_CATEGORY } from '../support/sharedLiveDocument';

const TARGET = 'Target sentence the reader comments on, in the middle of the document.';
const TAG = '@flow:doc-comment-dismiss-reading-place @surface:documents';
const promoted: string[] = [];
test.afterEach(async ({ request }) => { for (const id of promoted.splice(0)) await demoteAgent(request, id); });

function documentText() {
	const lines = ['# Reading position probe', '', '**Status:** probe', ''];
	for (let i = 0; i < 40; i++) {
		lines.push(`## Section ${i}`, '', `Paragraph ${i}: a public-safe paragraph long enough to wrap onto several rows on a phone, so the document scrolls a long way.`, '');
		if (i % 5 === 2) lines.push('| Column A | Column B |', '| --- | --- |', `| row ${i} a | row ${i} b |`, `| row ${i} c | row ${i} d |`, '');
		if (i % 3 === 1) lines.push(`![Shot ${i}](shots/shot-${i % 4}.png)`, '');
		if (i === 22) lines.push(TARGET, '');
	}
	return lines.join('\n');
}

/** Model a browser revealing its current caret in a later task after editable focus. */
async function delayedCaretReveal(page: Page) {
	await page.addInitScript(() => {
		const nativeFocus = HTMLElement.prototype.focus;
		(window as unknown as { caretRevealArmed: boolean }).caretRevealArmed = false;
		HTMLElement.prototype.focus = function (options) {
			nativeFocus.call(this, options);
			if (!(window as unknown as { caretRevealArmed: boolean }).caretRevealArmed || !this.matches('.doc .cm-content[contenteditable="true"]')) return;
			const scroller = this.closest<HTMLElement>('.cm-scroller');
			if (scroller) setTimeout(() => {
				const selection = document.getSelection();
				const range = selection?.rangeCount ? selection.getRangeAt(0) : null;
				if (!range || !this.contains(range.startContainer)) return;
				const caret = range.getBoundingClientRect(), viewport = scroller.getBoundingClientRect();
				if (caret.top < viewport.top || caret.bottom > viewport.bottom) scroller.scrollBy(0, caret.top - viewport.top);
			}, 0);
		};
	});
}

for (const width of [1440, 393]) {
	test.describe(`${width} touch`, () => {
		test.use({ viewport: { width, height: width === 1440 ? 900 : 852 }, hasTouch: true, isMobile: width === 393, video: 'off' });
		for (const exit of ['outside', 'escape', 'close'] as const) {
			test(`${TAG} ${width} ${exit} holds the reading place`, async ({ browser, page, request }, testInfo) => {
				test.skip(testInfo.project.name !== 'chromium-desktop');
				test.setTimeout(180_000);
				await delayedCaretReveal(page);
				const agentContext = await browser.newContext({ viewport: { width: 1440, height: 900 } });
				try {
					const agent = await openAgentBeside(request, await agentContext.newPage(), `Reading place ${Date.now()}`, promoted);
					const tree = await request.get('/api/files/tree', { params: { token: TEST_TOKEN } });
					const workspace = ((await tree.json()).roots as Array<{ kind: string; path: string }>).find(root => root.kind === 'workspace')!.path;
					const shots = path.join(workspace, 'shared', SHARED_CATEGORY, 'shots');
					fs.mkdirSync(shots, { recursive: true });
					for (let i = 0; i < 4; i++) fs.writeFileSync(path.join(shots, `shot-${i}.png`), syntheticPng(640 + i * 80, 400 + i * 120));
					const live = await agentCreatesSharedDocument(request, agent, `reading-place-${width}`, documentText());
					const reviewer = await reviewerSession(request, `reading-place-${Date.now()}`);
					promoted.push(reviewer.id);
					await agentComment(request, live, reviewer.id, 'Paragraph 3: a public', 'An earlier comment.');
					await openLiveDocument(page, live, '# Reading position probe');
					const line = page.locator('.cm-line', { hasText: TARGET });
					await expect.poll(async () => {
						if (await line.count()) return true;
						await page.locator('.doc .cm-scroller').evaluate(el => el.scrollBy(0, el.clientHeight / 2));
						return false;
					}, { intervals: [80], timeout: 20_000 }).toBe(true);
					await line.scrollIntoViewIfNeeded();
					const offset = await line.evaluate(el => el.getBoundingClientRect().top - el.closest('.cm-scroller')!.getBoundingClientRect().top - 200);
					await page.locator('.doc .cm-scroller').evaluate((el, delta) => el.scrollBy(0, delta), offset);
					await page.evaluate(target => {
						const content = document.querySelector('.doc .cm-content') as HTMLElement & { cmTile: { root: { view: { state: { doc: { toString(): string } }; focus(): void; dispatch(t: unknown): void } } } };
						const view = content.cmTile.root.view, at = view.state.doc.toString().indexOf(target) + target.indexOf('sentence');
						view.focus(); view.dispatch({ selection: { anchor: at, head: at + 'sentence'.length }, userEvent: 'select' });
					}, TARGET);
					await page.locator('.sel-comment').tap();
					const box = page.getByPlaceholder('Comment, or @ to tag an agent');
					await expect(box).toBeVisible();
					await box.fill('A draft comment.');
					const scroller = page.locator('.doc .cm-scroller');
					const before = await scroller.evaluate(el => el.scrollTop);
					expect(before).toBeGreaterThan(3000);
					await page.evaluate(() => { (window as unknown as { caretRevealArmed: boolean }).caretRevealArmed = true; });
					if (exit === 'outside') {
						const point = await page.evaluate(() => {
							const scroller = document.querySelector<HTMLElement>('.doc .cm-scroller')!, rect = scroller.getBoundingClientRect();
							for (let y = Math.max(rect.top + 12, 0); y < Math.min(rect.bottom - 12, innerHeight); y += 20) for (const dx of [4, 8, 12, 16]) {
								const x = rect.left + dx, stack = document.elementsFromPoint(x, y), hit = stack[0], underlying = stack.find(el => scroller.contains(el));
								if (hit && underlying && (scroller.contains(hit) || hit.matches('.drawer-backdrop')) && !underlying.closest('.cm-comment-anchor, .cm-comment-pending') && !hit.closest('[data-popover], .drawer-panel, button')) return { x, y };
							}
							throw new Error('No unobscured document gutter point');
						});
						await page.touchscreen.tap(point.x, point.y);
					} else if (exit === 'escape') await page.keyboard.press('Escape');
					else if (width === 393) await page.locator('.drawer-panel').getByRole('button', { name: 'Close', exact: true }).tap();
					else await page.getByRole('button', { name: 'Cancel', exact: true }).tap();
					await expect(box).toBeHidden();
					await expect.poll(async () => Math.abs((await scroller.evaluate(el => el.scrollTop)) - before), { timeout: 3_000 }).toBeLessThanOrEqual(3);
					// The delayed reveal must have had a chance to run before the final measurement.
					await page.waitForTimeout(500);
					expect(Math.abs((await scroller.evaluate(el => el.scrollTop)) - before)).toBeLessThanOrEqual(3);
				} finally { await agentContext.close(); }
			});
		}
	});
}
