import fs from 'node:fs';
import path from 'node:path';
import { expect, type Locator, type Page, type TestInfo } from '@playwright/test';
import { test } from '../support/test';
import { openComments } from '../support/commentDrawer';
import { setDebugVersionOverlay, setMockCorsiaAgentModel } from '../support/heldComments';
import { openLoaded, seedLoaded, type LoadedDocument, type LoadedView } from '../support/loadedComments';
import { settled } from '../support/reviewFixtures';

/**
 * The comment drawer at full load (support/loadedComments.ts), at every width it ships at, in both themes. The permanent
 * gate for the drawer's structure: on a phone the sheet stays on screen, keyboard up or down; its header with Accept all is
 * always reachable; the list is the one thing that scrolls; a long thread collapses in the list and opens whole in the same
 * sheet with Back; long text wraps; touch targets are 44 px; no comment card scrolls inside itself. Desktop widths keep their
 * own layout and must not lose Accept all, wrap or scrolling. Each case records what it measured, so a failing run says every
 * defect at once. At 852 and 900 the Workspace panel, when open, slides the main view aside and covers it (the shell's narrow
 * layout), so only the panel-closed state shows the drawer there; at 1440 the panel sits beside the canvas, open or closed, and a
 * widened canvas brings the margin column.
 */

type Case = { label: string; view: LoadedView };
const themes = ['light', 'dark'] as const;
const CASES: Case[] = [
	...themes.map((theme) => ({ label: `393x852 ${theme}`, view: { width: 393, height: 852, theme, touch: true, pane: 'open' as const } })),
	...themes.map((theme) => ({ label: `320x640 ${theme}`, view: { width: 320, height: 640, theme, touch: true, pane: 'open' as const } })),
	...themes.map((theme) => ({ label: `852x393 ${theme}`, view: { width: 852, height: 393, theme, touch: true, pane: 'closed' as const } })),
	...themes.map((theme) => ({ label: `900 pane-closed ${theme}`, view: { width: 900, height: 900, theme, touch: false, pane: 'closed' as const } })),
	...themes.flatMap((theme) => (['open', 'closed'] as const).map((pane) => ({ label: `1440 pane-${pane} ${theme}`, view: { width: 1440, height: 900, theme, touch: false, pane } }))),
	...themes.map((theme) => ({ label: `1440 wide-canvas ${theme}`, view: { width: 1440, height: 900, theme, touch: false, pane: 'closed' as const, canvas: 'wide' as const } })),
];

const DRAWER = '[data-doc-comments-drawer]';
const evidenceDir = process.env.LOADED_EVIDENCE_DIR;

/** What one look at the drawer found. */
type Look = {
	/** The sheet or column runs past the visual viewport by this many px (0 when it is on screen). */
	offscreen: number;
	/** Every element in the drawer that scrolls on its own right now. */
	scrollers: string[];
	/** Elements inside a comment card that scroll on their own: a card is never a scroller. */
	cardScrollers: string[];
	/** Elements drawn past the drawer's sides: content cut or the sheet widened. */
	wide: string[];
	/** Each Accept all control and whether it is on screen and takes a tap at its centre. */
	acceptAll: { name: string; reachable: boolean }[];
	/** Touch controls under 44 px. */
	small: string[];
};

function look(page: Page, touch: boolean): Promise<Look> {
	return page.evaluate(({ selector, touch }) => {
		const drawer = document.querySelector<HTMLElement>(selector);
		if (!drawer) throw new Error('the comment drawer is not open');
		const container = drawer.closest<HTMLElement>('.drawer-panel, [data-popover]') ?? drawer;
		const viewport = window.visualViewport;
		const vTop = viewport?.offsetTop ?? 0, vHeight = viewport?.height ?? window.innerHeight, vWidth = viewport?.width ?? window.innerWidth;
		const name = (el: Element) => `${el.tagName.toLowerCase()}${el.classList.length ? '.' + [...el.classList].filter((c) => !c.startsWith('svelte-')).join('.') : ''}${el.getAttribute('aria-label') ? `[${el.getAttribute('aria-label')}]` : ''}`;
		const box = container.getBoundingClientRect();
		// The margin column lives in the document's own scroller, so only a sheet or popover is held to the viewport.
		const floating = container !== drawer || !drawer.classList.contains('margin');
		const offscreen = floating ? Math.max(0, Math.round(box.bottom - (vTop + vHeight)), Math.round(vTop - box.top), Math.round(box.right - vWidth), Math.round(-box.left)) : 0;
		const all = [container, ...container.querySelectorAll<HTMLElement>('*')];
		const visible = (el: Element) => (el as HTMLElement).checkVisibility?.({ checkOpacity: true, checkVisibilityCSS: true }) ?? true;
		const scrolls = (el: Element) => /auto|scroll/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight + 1;
		const scrollers = all.filter((el) => visible(el) && scrolls(el)).map(name);
		const cardScrollers = [...new Set([...container.querySelectorAll('[data-comment-id], [data-comment-id] *')].filter(scrolls).map(name))];
		const clipped = (el: Element) => getComputedStyle(el).clipPath !== 'none';
		const wide = [...new Set(all.filter((el) => {
			if (!visible(el) || clipped(el)) return false;
			const rect = el.getBoundingClientRect();
			if (rect.width < 2 || rect.height < 2) return false;
			return rect.right > box.right + 1 || rect.left < box.left - 1 || el.scrollWidth > el.clientWidth + 1 && /auto|scroll/.test(getComputedStyle(el).overflowX);
		}).map(name))].slice(0, 8);
		const acceptAll = [...container.querySelectorAll<HTMLButtonElement>('button')].filter((b) => /^Accept all/.test(b.textContent?.trim() ?? '')).map((button) => {
			const rect = button.getBoundingClientRect();
			const onScreen = rect.height > 0 && rect.top >= vTop && rect.bottom <= vTop + vHeight && rect.left >= 0 && rect.right <= vWidth;
			const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
			return { name: button.textContent!.trim(), reachable: onScreen && !!hit && button.contains(hit) };
		});
		const target = (el: HTMLElement) => {
			const rect = el.getBoundingClientRect();
			let { width, height } = rect;
			// A control whose ::before is its larger hit area (an image comment's number) counts that area.
			const before = getComputedStyle(el, '::before');
			if (before.content !== 'none' && before.position === 'absolute') {
				const inset = (side: string) => -(parseFloat(before.getPropertyValue(side)) || 0);
				width += inset('left') + inset('right'); height += inset('top') + inset('bottom');
			}
			return Math.min(width, height);
		};
		const small = touch ? [...new Set(all.filter((el) => el.matches('button, a[href], textarea, summary, [role="button"]') && visible(el) && !clipped(el)
			&& el.getBoundingClientRect().height > 0 && el.getBoundingClientRect().bottom > vTop && el.getBoundingClientRect().top < vTop + vHeight
			&& !el.closest('.cm-editor') && target(el) < 43.5).map((el) => `${name(el)} ${Math.round(el.getBoundingClientRect().width)}x${Math.round(el.getBoundingClientRect().height)}`))].slice(0, 12) : [];
		return { offscreen, scrollers, cardScrollers, wide, acceptAll, small };
	}, { selector: DRAWER, touch });
}

/** The drawer's main scroller, scrolled to its end. */
async function scrollListToEnd(page: Page) {
	await page.evaluate((selector) => {
		const drawer = document.querySelector<HTMLElement>(selector)!;
		const container = drawer.closest<HTMLElement>('.drawer-panel, [data-popover]') ?? drawer;
		const scrollers = [container, ...container.querySelectorAll<HTMLElement>('*')].filter((el) => /auto|scroll/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight + 1);
		const main = scrollers.sort((a, b) => b.clientHeight - a.clientHeight)[0];
		if (main) main.scrollTop = main.scrollHeight;
		else document.querySelector<HTMLElement>('.cm-scroller')?.scrollTo({ top: 1e6 });
	}, DRAWER);
	await settled(page);
}

const shot = async (page: Page, info: TestInfo, label: string, name: string) => {
	const file = `${label.replace(/[^a-z0-9]+/gi, '-')}--${name}.png`;
	await page.screenshot({ path: info.outputPath(file), animations: 'disabled' });
	if (evidenceDir) { fs.mkdirSync(evidenceDir, { recursive: true }); fs.copyFileSync(info.outputPath(file), path.join(evidenceDir, file)); }
};

const card = (page: Page, id: string): Locator => page.locator(`${DRAWER} [data-comment-id="${id}"]`).first();

/** Scrolls each reply's author line into view through whatever scrolls it, and names those that then are not on screen and hit. */
function hiddenAuthorLines(thread: Locator): Promise<string[]> {
	return thread.locator('.ct-reply .ct-msg-meta').evaluateAll((metas) => metas.flatMap((meta, index) => {
		meta.scrollIntoView({ block: 'nearest', behavior: 'instant' });
		const rect = meta.getBoundingClientRect(), vv = window.visualViewport;
		const top = vv?.offsetTop ?? 0, height = vv?.height ?? window.innerHeight;
		const hit = document.elementFromPoint(rect.x + Math.min(8, rect.width / 2), rect.y + rect.height / 2);
		return rect.height > 0 && rect.top >= top - 1 && rect.bottom <= top + height + 1 && !!hit && meta.contains(hit) ? [] : [`reply ${index + 1} (${meta.textContent?.trim().slice(0, 30)})`];
	}));
}

/** Scrolls a comment body into view through whatever scrolls it, then names the first line of it that is off screen or cut by a box; null when every line is whole. */
function bodyCut(body: Locator): Promise<string | null> {
	return body.evaluate((root) => {
		root.scrollIntoView({ block: 'start', behavior: 'instant' });
		// The view is the screen and the one scroller holding the card (the sheet, popover or column): lines past its edge are
		// read by scrolling it. Only a box inside the card cutting a line counts.
		const card = root.closest<HTMLElement>('.ct') ?? root;
		let outer = card.parentElement;
		while (outer && !/(auto|scroll)/.test(getComputedStyle(outer).overflowY)) outer = outer.parentElement;
		const vv = window.visualViewport, screenTop = vv?.offsetTop ?? 0, screenBottom = screenTop + (vv?.height ?? window.innerHeight);
		const held = outer?.getBoundingClientRect();
		const top = Math.max(screenTop, held?.top ?? screenTop), bottom = Math.min(screenBottom, held?.bottom ?? screenBottom);
		const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
		for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
			if (!node.data.trim()) continue;
			const range = document.createRange();
			range.selectNodeContents(node);
			for (const rect of range.getClientRects()) {
				if (!rect.height) continue;
				const line = `line ${Math.round(rect.top)}-${Math.round(rect.bottom)} ("${node.data.trim().slice(0, 20)}")`;
				// A body taller than the view is read in turn: only lines that start inside the view are held to it.
				if (rect.top >= bottom) return null;
				if (rect.top < top - 1 || rect.bottom > bottom + 1) return `${line} off screen`;
				for (let parent = node.parentElement; parent && parent !== card.parentElement; parent = parent.parentElement) {
					if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(parent).overflowY)) continue;
					const clip = parent.getBoundingClientRect();
					if (rect.top < clip.top - 1 || rect.bottom > clip.bottom + 1) return `${line} cut by ${parent.className || parent.tagName}`;
				}
			}
		}
		return null;
	});
}

/** Whether a field is on screen and takes a tap at its centre. */
function onScreen(field: Locator): Promise<boolean> {
	return field.evaluate((el) => {
		const rect = el.getBoundingClientRect(), vv = window.visualViewport;
		const top = vv?.offsetTop ?? 0, height = vv?.height ?? window.innerHeight;
		const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
		return rect.top >= top && rect.bottom <= top + height && !!hit && el.contains(hit);
	});
}

for (const { label, view } of CASES) {
	test.describe(label, () => {
		if (view.touch) test.use({ hasTouch: true, isMobile: true });
		test(`@flow:doc-comments-loaded the comment drawer holds up fully loaded (${label})`, async ({ page, request }, info) => {
			test.skip(info.project.name !== 'chromium-desktop', 'every size is set per case on the desktop project');
			test.setTimeout(240_000);
			await setMockCorsiaAgentModel(request);
			await setDebugVersionOverlay(request, false);
			const doc: LoadedDocument = await seedLoaded(request, label);
			await openLoaded(page, doc, view);
			const failures: string[] = [];
			const press = (target: Locator) => view.touch ? target.tap() : target.click();
			const report: Record<string, unknown> = { label, view };
			const fail = (message: string) => failures.push(message);

			// The list, at its top.
			await openComments(page);
			await expect(page.locator(`${DRAWER} [data-comment-id]`).first()).toBeVisible();
			await settled(page);
			await shot(page, info, label, '1-list');
			// The list is a sheet on a phone and on any canvas too narrow for the margin column; the column's cards rest without replies.
			const sheet = await page.locator(DRAWER).evaluate((el) => !!el.closest('.drawer-panel'));
			const column = await page.locator(DRAWER).evaluate((el) => el.classList.contains('margin'));
			report.form = sheet ? 'sheet' : column ? 'margin column' : 'drawer';
			const top = await look(page, view.touch);
			report.top = top;
			if (top.offscreen) fail(`list: the drawer runs ${top.offscreen}px off screen`);
			if (top.acceptAll.length < 2) fail(`list: ${top.acceptAll.length} Accept all controls, expected one per agent`);
			for (const control of top.acceptAll) if (!control.reachable) fail(`list top: "${control.name}" can't be reached`);
			if (top.scrollers.length > 1) fail(`list: ${top.scrollers.length} areas scroll (${top.scrollers.join(', ')})`);
			if (top.cardScrollers.length) fail(`list: a comment card scrolls inside itself (${top.cardScrollers.join(', ')})`);
			if (top.wide.length) fail(`list: content past the drawer's edge (${top.wide.join(', ')})`);
			if (top.small.length) fail(`list: touch targets under 44px (${top.small.join('; ')})`);

			// A long thread's replies are the agents', unread to the person: every one shows, none behind a replies row.
			const longCard = card(page, doc.longThreads[1]);
			await longCard.scrollIntoViewIfNeeded();
			const longReplies = await longCard.locator('.ct-reply').count();
			report.longThreadRepliesShown = longReplies;
			if (!column && longReplies !== 16) fail(`list: a thread with 16 unread replies shows ${longReplies}, expected all 16`);
			// A long suggestion shows whole when it fits its clamp (a wide sheet wraps it in fewer lines); past the clamp it is cut with Show all.
			const suggestion = card(page, doc.longSuggestion);
			await suggestion.scrollIntoViewIfNeeded();
			const showAll = suggestion.getByRole('button', { name: 'Show all' });
			report.suggestionHeight = Math.round((await suggestion.boundingBox())?.height ?? 0);
			const diffCut = await suggestion.locator('.ct-diff').evaluate((el) => el.scrollHeight > el.clientHeight + 1);
			report.suggestionClamped = diffCut;
			if (diffCut && !(await showAll.count())) fail(`list: the 400+ character suggestion is cut with no Show all (${report.suggestionHeight}px tall)`);
			// Long text past chat's collapse length: unread shows whole; the person's own, read, collapses with Show more.
			const unread = card(page, doc.longUnreadThread);
			await unread.scrollIntoViewIfNeeded();
			if (await unread.getByRole('button', { name: 'Show more' }).count()) fail('list: the long unread comment is collapsed behind Show more');
			const read = card(page, doc.longReadThread);
			await read.scrollIntoViewIfNeeded();
			if (!(await read.locator('[data-discussion-original]').getByRole('button', { name: 'Show more' }).count())) fail('list: the long comment the person has read does not collapse');
			if (await read.locator('.ct-reply').getByRole('button', { name: 'Show more' }).count()) fail('list: the long unread reply is collapsed behind Show more');

			// The list at its end: Accept all is still there.
			await scrollListToEnd(page);
			await shot(page, info, label, '2-list-end');
			const end = await look(page, view.touch);
			report.end = end;
			for (const control of end.acceptAll) if (!control.reachable && sheet) fail(`list end: "${control.name}" can't be reached`);
			if (end.offscreen) fail(`list end: the drawer runs ${end.offscreen}px off screen`);

			// From the list into a long thread and back (the sheet's journey).
			if (sheet) {
				const opener = card(page, doc.longThreads[0]);
				await opener.scrollIntoViewIfNeeded();
				const row = opener.getByRole('button', { name: /16 replies/ });
				if (await row.count()) await press(row);
				else await press(opener.locator('.ct').first());
				await settled(page);
				await shot(page, info, label, '3-thread');
				const back = page.locator(DRAWER).getByRole('button', { name: /^Back/ });
				const drawerOpen = await page.locator(DRAWER).count();
				report.threadView = { drawerOpen: !!drawerOpen, back: drawerOpen ? await back.count() : 0 };
				if (!drawerOpen) {
					fail('thread: tapping a long thread closes the sheet instead of opening the thread');
				} else {
					const inThread = await look(page, view.touch);
					report.thread = inThread;
					if (!(await back.count()) || !(await back.isVisible())) fail('thread: no Back control to return to the list');
					if (inThread.offscreen) fail(`thread: the sheet runs ${inThread.offscreen}px off screen`);
					if (inThread.scrollers.length > 1) fail(`thread: ${inThread.scrollers.length} areas scroll (${inThread.scrollers.join(', ')})`);
					if (inThread.cardScrollers.length) fail(`thread: the comment card scrolls inside itself (${inThread.cardScrollers.join(', ')})`);
					if (inThread.wide.length) fail(`thread: content past the sheet's edge (${inThread.wide.join(', ')})`);
					const shown = await page.locator(`${DRAWER} [data-comment-id="${doc.longThreads[0]}"] .ct-reply`).count();
					if (shown < 16) fail(`thread: the opened thread shows ${shown} of its 16 replies`);
					const hiddenAuthors = await hiddenAuthorLines(page.locator(`${DRAWER} [data-comment-id="${doc.longThreads[0]}"]`));
					if (hiddenAuthors.length) fail(`thread: author lines not visible when scrolled to (${hiddenAuthors.slice(0, 4).join('; ')})`);
					// The reply field is not pinned: it is the end of the one scroller, and reached by scrolling it.
					const reply = page.locator(DRAWER).getByPlaceholder('Reply…').last();
					const fieldOnScreen = () => onScreen(reply);
					await scrollListToEnd(page);
					if (!(await reply.count()) || !(await fieldOnScreen())) fail('thread: the reply field is not reached at the end of the thread');
					// The keyboard comes up: the page's visible height shrinks by the keyboard's.
					if (view.touch && (await reply.count())) {
						await reply.tap();
						await page.setViewportSize({ width: view.width, height: Math.round(view.height * 0.58) });
						await settled(page);
						await page.waitForTimeout(300);
						await shot(page, info, label, '4-thread-keyboard');
						const typing = await look(page, view.touch);
						report.keyboard = typing;
						if (typing.offscreen) fail(`keyboard: the sheet runs ${typing.offscreen}px off screen`);
						if (typing.cardScrollers.length) fail(`keyboard: the comment card scrolls inside itself (${typing.cardScrollers.join(', ')})`);
						if (!(await fieldOnScreen())) fail('keyboard: the reply field is hidden');
						if (await back.count() && !(await back.isVisible())) fail('keyboard: Back is not visible');
						// The reply grows while it is typed: the comment it answers stays readable whole, every line of its body
						// reached by scrolling the sheet and none cut off by a box inside the card.
						for (let i = 0; i < 8; i++) {
							await page.keyboard.type(`growing reply line ${i}`);
							if (i < 7) await page.keyboard.press('Shift+Enter');
						}
						await settled(page);
						const thread = page.locator(`${DRAWER} [data-comment-id="${doc.longThreads[0]}"]`);
						if ((await thread.locator('.ct-input').evaluate((el) => (el as HTMLTextAreaElement).value.split('\n').length).catch(() => 0)) < 8) fail('typing: the reply did not take the typed lines');
						const typed = await look(page, view.touch);
						if (typed.cardScrollers.length) fail(`typing: the comment card scrolls inside itself (${typed.cardScrollers.join(', ')})`);
						// The caret (at the end of the text) and Send stay in view while the reply grows.
						const caretAndSend = await thread.evaluate((root) => {
							const field = root.querySelector<HTMLTextAreaElement>('.ct-input'), send = root.querySelector<HTMLElement>('[data-send-reply]');
							const vv = window.visualViewport, top = vv?.offsetTop ?? 0, bottom = top + (vv?.height ?? window.innerHeight);
							const line = field ? Number.parseFloat(getComputedStyle(field).lineHeight) || 20 : 0;
							const f = field?.getBoundingClientRect(), s = send?.getBoundingClientRect();
							const problems = [];
							if (!f || f.bottom > bottom + 1 || f.bottom - line < top - 1) problems.push('caret line out of view');
							if (!s || s.top < top - 1 || s.bottom > bottom + 1) problems.push('Send out of view');
							return problems.join(', ');
						});
						if (caretAndSend) fail(`typing: ${caretAndSend}`);
						const cut = await bodyCut(thread.locator('[data-discussion-original]').first());
						if (cut) fail(`typing: the comment body is cut off while the reply grows (${cut})`);
						await shot(page, info, label, '4b-thread-typing');
						await page.keyboard.press('Escape').catch(() => {});
						await page.locator(DRAWER).getByPlaceholder('Reply…').last().blur().catch(() => {});
						await page.setViewportSize({ width: view.width, height: view.height });
						await settled(page);
					}
					if (await back.count()) {
						await press(back);
						await settled(page);
						await shot(page, info, label, '5-back');
						const after = await look(page, view.touch).catch(() => null);
						report.back = after;
						if (!after) fail('back: Back closed the sheet instead of returning to the list');
						else for (const control of after.acceptAll) if (!control.reachable) fail(`back: "${control.name}" can't be reached`);
						if (!(await page.locator(`${DRAWER} [data-comment-id]`).nth(3).count())) fail('back: the list did not come back');
					}
				}
			}

			report.failures = failures;
			fs.writeFileSync(info.outputPath('report.json'), JSON.stringify(report, null, 2));
			if (evidenceDir) fs.writeFileSync(path.join(evidenceDir, `${label.replace(/[^a-z0-9]+/gi, '-')}--report.json`), JSON.stringify(report, null, 2));
			expect(failures, `the fully loaded drawer at ${label}`).toEqual([]);
		});
	});
}

/**
 * Long text, opened from its place in the document: on a phone (the sheet), at 1440 beside the chat (the popover, the canvas
 * too narrow for the margin) and at 1440 with the canvas widened (the margin card). An unread comment and its unread reply,
 * both past chat's collapse length, show whole with no Show more, and stay whole once read while open; every reply's author
 * line shows when scrolled to; nothing in the card scrolls on its own. The person's own long comment, read, collapses.
 */
const LONG_CASES: Case[] = [
	{ label: '393x852', view: { width: 393, height: 852, theme: 'light', touch: true, pane: 'open' } },
	{ label: '320x700', view: { width: 320, height: 700, theme: 'light', touch: true, pane: 'open' } },
	{ label: '852x393', view: { width: 852, height: 393, theme: 'light', touch: true, pane: 'closed' } },
	{ label: '1440 popover', view: { width: 1440, height: 900, theme: 'light', touch: false, pane: 'open' } },
	{ label: '1440 margin', view: { width: 1440, height: 900, theme: 'light', touch: false, pane: 'closed', canvas: 'wide' } },
];

for (const { label, view } of LONG_CASES) {
	test.describe(`long text ${label}`, () => {
		if (view.touch) test.use({ hasTouch: true, isMobile: true });
		test(`@flow:doc-comments-loaded long unread text shows whole and long read text collapses (${label})`, async ({ page, request }, info) => {
			test.skip(info.project.name !== 'chromium-desktop', 'every size is set per case on the desktop project');
			test.setTimeout(240_000);
			await setMockCorsiaAgentModel(request);
			await setDebugVersionOverlay(request, false);
			const doc = await seedLoaded(request, `long ${label}`);
			await openLoaded(page, doc, view);
			const press = (target: Locator) => view.touch ? target.tap() : target.click();
			const failures: string[] = [];
			const fail = (message: string) => failures.push(message);
			const report: Record<string, unknown> = { label, view };
			const open = async (quote: string, id: string) => {
				const anchor = page.locator('.cm-comment-anchor').filter({ hasText: quote }).first();
				// The editor draws only the lines near its view: scroll the document down until the quote's line is drawn.
				const scroller = page.locator('.cm-scroller').first();
				await scroller.evaluate((el) => { el.scrollTop = 0; });
				for (let step = 0; step < 40 && !(await anchor.count()); step++) {
					await scroller.evaluate((el) => { el.scrollTop += el.clientHeight * 0.8; });
					await page.waitForTimeout(50);
				}
				await anchor.evaluate((el) => el.scrollIntoView({ block: 'center', behavior: 'instant' }));
				await press(anchor);
				// One thread opens on its own in the sheet or the popover; in the margin it is that comment's card in the column.
				const thread = page.locator(`.drawer-panel div.ct, [data-popover] div.ct, [data-comment-id="${id}"] div.ct`).first();
				await expect(thread).toBeVisible();
				await settled(page);
				return thread;
			};
			const inner = (thread: Locator) => thread.evaluate((root) => [root, ...root.querySelectorAll<HTMLElement>('*')]
				.filter((el) => /auto|scroll/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight + 1)
				.map((el) => `${el.tagName.toLowerCase()}.${[...el.classList].filter((c) => !c.startsWith('svelte-')).join('.')}`));
			const thread = await open('hotel milestone', doc.longUnreadThread);
			report.form = await thread.evaluate((el) => el.closest('.drawer-panel') ? 'sheet' : el.closest('[data-popover]') ? 'popover' : el.closest('.margin') ? 'margin' : 'other');
			await shot(page, info, `long-${label}`, '1-unread');
			const unreadInner = await inner(thread);
			report.innerScrollers = unreadInner;
			if (unreadInner.length) fail(`unread: the card scrolls inside itself (${unreadInner.join(', ')})`);
			if (await thread.getByRole('button', { name: 'Show more' }).count()) fail('unread: the long unread comment is behind Show more');
			// Read while open (its New mark clears): it stays whole under the reader.
			await page.waitForTimeout(2_500);
			await shot(page, info, `long-${label}`, '2-unread-read');
			if (await thread.getByRole('button', { name: 'Show more' }).count()) fail('unread: the comment collapsed once read while open');

			await page.keyboard.press('Escape');
			await settled(page);
			const own = await open('Results load within the budget', doc.longReadThread);
			await shot(page, info, `long-${label}`, '3-read');
			const ownInner = await inner(own);
			if (ownInner.length) fail(`read: the card scrolls inside itself (${ownInner.join(', ')})`);
			if (!(await own.locator('[data-discussion-original]').getByRole('button', { name: 'Show more' }).count())) fail('read: the person\'s own long comment does not collapse');
			if (!(await own.locator('.ct-reply').count())) fail('read: the unread reply does not show');
			if (await own.locator('.ct-reply').getByRole('button', { name: 'Show more' }).count()) fail('read: the long unread reply is behind Show more');
			const hidden = await hiddenAuthorLines(own);
			if (hidden.length) fail(`read: author lines not visible when scrolled to (${hidden.join('; ')})`);
			await page.waitForTimeout(2_500);
			if (await own.locator('.ct-reply').getByRole('button', { name: 'Show more' }).count()) fail('read: the reply collapsed once read while open');

			report.failures = failures;
			fs.writeFileSync(info.outputPath('report.json'), JSON.stringify(report, null, 2));
			if (evidenceDir) fs.writeFileSync(path.join(evidenceDir, `long-${label.replace(/[^a-z0-9]+/gi, '-')}--report.json`), JSON.stringify(report, null, 2));
			expect(failures, `long text at ${label}`).toEqual([]);
		});
	});
}
