import fs from 'node:fs';
import os from 'node:os';
import path from 'node:path';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page, type Route, type TestInfo } from '@playwright/test';
import { chatComposer, createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { injectMessage, seedMockResponses } from '../support/mock';
import { showAllThreads } from '../support/commentDrawer';

/**
 * Evidence frames and recordings for four comment fixes, from the real components on a disposable instance: a card opened
 * at the click, suggestions inside table cells, a margin marker for a comment whose text was deleted, and a decided
 * suggestion that keeps its replies. Frames go to C36_OUT/C36_TAG (TAG is `before` on the base checkout, `after` on the build).
 */
const OUT = process.env.C36_OUT ?? '', TAG = process.env.C36_TAG ?? 'after', AFTER = TAG === 'after';
const params = { token: TEST_TOKEN };
type Theme = 'light' | 'dark';
const CASES: ReadonlyArray<readonly [number, Theme]> = [[1440, 'light'], [1440, 'dark'], [393, 'light'], [393, 'dark']];

interface Doc { threadId: string; title: string; name: string; docPath: string; artifactId: string; text: string }

/** Threads this test made, deleted afterwards so the fixed titles never collide in the sidebar. */
const createdThreads: string[] = [];
test.afterEach(async ({ request }) => {
	for (const id of createdThreads.splice(0)) await request.delete(`/api/threads/${id}`, { params });
});
/** Every frame shows the same plain title; each document sits in its own folder so the fixed file name never collides. */
async function seedDoc(request: APIRequestContext, base: string, text: string): Promise<Doc> {
	const threadId = await createSession(request, base);
	createdThreads.push(threadId);
	const roots = await request.get('/api/files/tree', { params });
	const workspace = (await roots.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path;
	const name = `${base.replace(/\s+/g, '-')}.md`, docPath = path.join(workspace, threadId.slice(0, 8), name);
	const seeded = await request.put('/api/files/content', { params, data: { path: docPath, text, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	return { threadId, title: base, name, docPath, text, artifactId: (await seeded.json()).artifact.id as string };
}
/** The reviewer is always called "Reviewer" and always gets the first avatar colour: a thread id picks the colour by hash, so make threads until one lands on it. */
async function reviewerThread(request: APIRequestContext): Promise<string> {
	const colourIndex = (id: string) => { let hash = 0; for (const char of id) hash = ((hash << 5) - hash + char.charCodeAt(0)) | 0; return Math.abs(hash) % 8; };
	for (let tries = 0; tries < 60; tries++) {
		const id = await createSession(request, 'Reviewer');
		if (colourIndex(id) === 0) { createdThreads.push(id); return id; }
		await request.delete(`/api/threads/${id}`, { params });
	}
	throw new Error('no reviewer thread landed on the first avatar colour');
}
async function rewrite(request: APIRequestContext, doc: Doc, text: string) {
	const put = await request.put('/api/files/content', { params, data: { path: doc.docPath, text, thread_id: doc.threadId } });
	expect(put.ok(), await put.text()).toBeTruthy();
}
async function personComment(request: APIRequestContext, doc: Doc, quote: string, body: string): Promise<string> {
	const from = doc.text.indexOf(quote);
	expect(from, quote).toBeGreaterThan(-1);
	const made = await request.post(`/api/artifacts/${doc.artifactId}/comments`, { params, data: { body, anchor_text: quote, from_off: from, to_off: from + quote.length, delivery_policy: 'immediate' } });
	expect(made.ok(), await made.text()).toBeTruthy();
	return (await made.json()).comment.id as string;
}
async function reviewerSuggestion(request: APIRequestContext, doc: Doc, reviewerId: string, quote: string, replacement: string, body: string): Promise<string> {
	const made = await request.post('/api/delegation/docs/comments', { params, data: { artifact_id: doc.artifactId, quote, replacement, body, as_session_id: reviewerId } });
	expect(made.ok(), await made.text()).toBeTruthy();
	const result = await made.json();
	return (result.comment ?? result).id as string;
}
async function reply(request: APIRequestContext, doc: Doc, parentId: string, body: string) {
	const made = await request.post(`/api/artifacts/${doc.artifactId}/comments`, { params, data: { body, parent_id: parentId, delivery_policy: 'immediate' } });
	expect(made.ok(), await made.text()).toBeTruthy();
}
async function decide(request: APIRequestContext, id: string, decision: 'accept' | 'reject') {
	const made = await request.post(`/api/comments/${id}/decision`, { params, data: { decision, all_from_author: false } });
	expect(made.ok(), await made.text()).toBeTruthy();
}

type Layout = 'split' | 'wide';
/** `wide`: the sidebar and the right panel are hidden and the document pane is as wide as the app lets it be (desktop only). */
async function openDoc(page: Page, doc: Doc, width: number, theme: Theme, expectText: string, opts: { wide?: boolean } = {}) {
	const phone = width < 600;
	await page.setViewportSize({ width, height: phone ? 852 : 900 });
	await page.addInitScript(({ doc, theme, wide }) => {
		localStorage.setItem('corsia_theme', theme);
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [doc.threadId]: {
			tabs: [{ instanceId: doc.threadId, artifactId: doc.artifactId, path: doc.docPath, title: doc.name, loadedRevision: null, latestRevision: null, status: 'loading' }],
			activeTabId: doc.threadId, visible: true, activeSurface: 'artifact',
		} }, splitWidth: wide ? 5000 : null }));
	}, { doc, theme, wide: !!opts.wide });
	await loginByUrl(page, { waitForComposer: false });
	if (phone) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').filter({ hasText: doc.title }).first().click();
	await expect(page.locator('.cm-content')).toContainText(expectText);
	await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
	if (opts.wide && !phone) {
		await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.getByRole('button', { name: 'Toggle panel' }).click();
		await page.waitForTimeout(700);
	}
}

/** Capture-only click marker: a 14 px ring drawn over the page at a point. It is not part of the app. */
async function mark(page: Page, point: { x: number; y: number }) {
	await page.evaluate(({ x, y }) => {
		document.getElementById('c36-click')?.remove();
		const ring = document.createElement('div');
		ring.id = 'c36-click';
		ring.setAttribute('style', `position:fixed;left:${x - 7}px;top:${y - 7}px;width:14px;height:14px;box-sizing:border-box;border:2px solid #e0115f;border-radius:50%;pointer-events:none;z-index:2147483647;`);
		document.body.appendChild(ring);
	}, point);
}
const unmark = (page: Page) => page.evaluate(() => document.getElementById('c36-click')?.remove());

/** The page's own build stamp, logged with every frame. With C36_EXPECT_HASH set, a stamp that is not exactly that commit (no dagger, no arrow) fails the capture before any file is written. */
async function stamp(page: Page, info: TestInfo, name: string) {
	const overlay = page.locator('.version-overlay');
	const read = async () => ((await overlay.textContent()) ?? '').replace(/\s+/g, ' ').trim();
	await expect.poll(read).toMatch(/^b [0-9a-f]{7}\S* · f [0-9a-f]{7}\S* · /);
	const text = await read();
	const expected = process.env.C36_EXPECT_HASH;
	if (expected) expect(text.startsWith(`b ${expected} · f ${expected} · `), `stamp "${text}" is not commit ${expected}`).toBe(true);
	note(info, { frame: name, stamp: text });
}
/** Nothing is loading, animating or about to move when the frame is taken. */
async function ready(page: Page) {
	await page.evaluate(() => document.fonts.ready);
	await expect(page.getByText(/Loading comments/)).toHaveCount(0);
	await expect.poll(() => page.evaluate(() => document.getAnimations().filter((a) => a.playState === 'running' && a.effect?.getComputedTiming().iterations !== Infinity).length)).toBe(0);
	await expect.poll(() => page.evaluate(() => Math.min(...[...document.querySelectorAll('[data-popover], [data-adaptive-drawer] .drawer-panel')].map((el) => Number(getComputedStyle(el).opacity)), 1))).toBe(1);
	await page.waitForTimeout(300);
}
async function frame(page: Page, info: TestInfo, name: string, width: number, theme: Theme) {
	await ready(page);
	await stamp(page, info, name);
	fs.mkdirSync(path.join(OUT, TAG), { recursive: true });
	let shot = await page.screenshot({ animations: 'disabled' });
	if (phoneOf(width)) {
		// A phone sheet must be at rest: two frames 300 ms apart have to match byte for byte.
		let tries = 1;
		for (; tries < 8; tries++) {
			await page.waitForTimeout(300);
			const next = await page.screenshot({ animations: 'disabled' });
			if (next.equals(shot)) break;
			shot = next;
		}
		note(info, { frame: name, width, theme, settledAfterFrames: tries, settled: tries < 8 });
	}
	fs.writeFileSync(path.join(OUT, TAG, `${name}-${info.project.name.replace('-desktop', '')}-${width}-${theme}.png`), shot);
}
function note(info: TestInfo, line: object) {
	fs.mkdirSync(path.join(OUT, TAG), { recursive: true });
	fs.appendFileSync(path.join(OUT, TAG, 'geometry.jsonl'), `${JSON.stringify({ engine: info.project.name, ...line })}\n`);
}
const phoneOf = (width: number) => width < 600;
const pointerAt = async (page: Page, width: number, point: { x: number; y: number }) => {
	if (phoneOf(width)) await page.touchscreen.tap(point.x, point.y); else await page.mouse.click(point.x, point.y);
};
const cardOf = (page: Page, width: number) => phoneOf(width) ? page.locator('[data-adaptive-drawer] .drawer-panel') : page.locator('[data-popover]');
async function closeCard(page: Page) {
	await page.keyboard.press('Escape');
	await expect(page.locator('[data-popover], [data-adaptive-drawer] .drawer-panel')).toHaveCount(0);
}
/** Runs a case at every width and theme: 1440 and 393, light and dark. With `wide`, 1440 also runs with the document filling the window. Chromium cross-checks 1440 light once. */
function cases(title: string, run: (args: { page: Page; request: APIRequestContext; info: TestInfo; width: number; theme: Theme; layout: Layout }) => Promise<void>, opts: { wide?: boolean; chromiumPhone?: boolean; chromiumAll?: boolean } = {}) {
	for (const [width, theme] of CASES) {
		for (const layout of (opts.wide && !phoneOf(width) ? ['split', 'wide'] : ['split']) as Layout[]) {
			const label = `${title} ${width} ${theme}${layout === 'wide' ? ' wide' : ''}`;
			test.describe(label, () => {
				if (phoneOf(width)) test.use({ hasTouch: true, isMobile: true });
				test(label, async ({ page, request }, info) => {
					test.setTimeout(120_000);
					test.skip(info.project.name === 'chromium-mobile', 'the viewport is set by the case');
					test.skip(info.project.name === 'chromium-desktop' && !opts.chromiumAll && !(width === 1440 && theme === 'light') && !(opts.chromiumPhone && phoneOf(width)), 'one Chromium cross-check per item');
					await run({ page, request, info, width, theme, layout });
				});
			});
		}
	}
}
const PARAS = [
	'The spring work season opens on April 6 with the crew leads walking the whole corridor before any tool comes out of the shed.',
	'Walking the corridor takes most of the first day, and the notes from it set the plan for the season. Start at the north trailhead and work south in pairs, one person watching the trail surface and the other watching the slopes above and below it. Photograph every problem with a landmark in the frame, record the mile marker, and rate it: safe to leave, needs repair this season, or needs a closure. Anything rated as a closure is reported to the coordinator before you leave the trail, not at the end of the day. Bring the notes to the briefing on the second Saturday so that the drainage, sign and bridge crews can divide the list between them and agree who needs which tools.',
	'Each lead records washouts, downed trees, failed tread and anything that blocks a wheelchair or a stroller, with a photo and a mile marker.',
	'The north trailhead sign was knocked over in the February windstorm and the replacement has not been ordered yet.',
	'Drainage comes first. Every culvert and rolling dip is cleared before the rains slow down, because blocked drains cause most of the damage we repair each summer.',
	'Volunteers sign in at the trailhead kiosk, take a safety briefing from their crew lead, and are paired with someone who has done the job before.',
	'The county permit for the Cedar Creek crossing was filed on March 3 and the reviewer has asked for a revised bridge drawing.',
	'We keep a running list of materials so the volunteer coordinator can order gravel, rebar and treated lumber in one delivery instead of four.',
	'Tool care matters. Every pulaski, McLeod and loppers pair is wiped down, sharpened and counted back in before the crew leaves the staging area.',
	'The accessible section between the lot and the overlook needs fresh decomposed granite, and the edging along the lower curve has heaved.',
	'Mile 4 is the weak point: the tread there is outsloped the wrong way and every storm sends water straight down the middle of the trail.',
	'Dana will lead the sign crew, Marcus the drainage crew, and Priya the bridge and boardwalk crew, with Sam floating between them.',
	'If a crew finds a hazard it cannot fix in a day, it flags the spot with tape, posts a notice at both ends and tells the coordinator the same evening.',
	'Lunch is provided on the first Saturday of each month. Everyone is welcome to stay for the debrief, which usually runs about twenty minutes.',
	'Weather calls are made by 6 pm the evening before. If the forecast shows more than half an inch of rain, the work day moves to Sunday.',
	'Photos from every work day go into the shared album so the board can see what was done and what is still open.',
	'The boardwalk at the wet meadow is due for new decking this year, and the old boards are being saved for the benches at the overlook.',
	'We will also resurface the parking lot entrance, which has potholes deep enough to damage a car and has already cost two volunteers a flat tire.',
	'Youth groups are welcome on the second work day of each month, with a lead assigned to every four students.',
	'Invasive species removal begins in May with the blackberry along the lower meadow, which crowds the tread every year.',
	'The annual report to the board will summarise hours worked, miles maintained and the condition of each structure.',
	'Safety first: no one works alone, everyone carries water, and every crew has a radio and a first aid kit.',
	'Bench installation at the overlook waits on the decking, so it moves to the second half of the season.',
	'The sign shop needs the final text for the five trailhead panels by the middle of April to make the June installation.',
	'We close each work day by walking the day\'s section end to end, so that no tool, stake or flag is left on the trail.',
];
const LONG_DOC = `# Spring trail maintenance plan\n\n${PARAS.join('\n\n')}\n`;

const TABLE_DOC = `# Trail work status

Crew assignments and permit status for the spring season.

| Section | Lead | Status | Notes |
| --- | --- | --- | --- |
| Trailhead signs | Dana | In progress | Replace the faded north sign |
| Drainage | Marcus | Blocked | Waiting on county permit before the crew can start, and the culvert order is still not confirmed with the supplier, so the second week of April is the earliest realistic start |
| Bridge | Priya | Done | Decking inspected |

## Crew schedule

| Wk | Crew | Day | Hrs | Lead | Due |
| --- | --- | --- | --- | --- | --- |
| 1 | North | Sat | 6 | Dana | Apr 12 |
| 2 | South | Sun | 4 | Marcus | Apr 19 |
| 3 | East | Sat | 6 | Priya | May 3 |
`;

const ORPHAN_DOC = `# Crew handbook

Every crew lead walks the section before work starts and writes down what needs fixing.

| Section | Lead | Status |
| --- | --- | --- |
| Signs | Dana | Waiting on county permit |
| Drainage | Marcus | In progress |

Volunteers must sign the waiver at the kiosk before they pick up a tool.

Tools are counted out in the morning and counted back in at the end of the day.

Radios are checked on the first Saturday of the month and the batteries are swapped.

First aid kits are restocked after every work day by the crew lead.

Water is carried by each volunteer; the crew lead carries two extra liters.

Lunch is provided on the first Saturday of each month.
`;

const DECIDED_DOC = `# Volunteer welcome letter

Thank you for joining the spring crew. Please arrive by 8:30 am at the north trailhead.

Bring water, work gloves and sturdy boots. We provide tools, hard hats and lunch.

If the weather turns, we will email everyone by 6 pm the evening before.

Questions can go to the volunteer coordinator at any time before the first work day.
`;

// ---- Fix 3: a card opens where you click ----
cases('fix3', async ({ page, request, info, width, theme }) => {
	const quotes = ['crew leads walking the whole corridor', 'rolling dip is cleared before the rains slow down, because blocked drains', 'tread there is outsloped the wrong way and every storm sends water straight down the middle'];
	const bodies = ['Name who walks which section so nothing is walked twice.', 'Does this include the rolling dips on the Ridge Loop?', 'Can we get a photo of this before the first storm?'];
	const doc = await seedDoc(request, 'Trail plan', LONG_DOC);
	for (const [index, quote] of quotes.entries()) await personComment(request, doc, quote, bodies[index]);
	await openDoc(page, doc, width, theme, 'spring work season');
	const scroller = page.locator('.cm-scroller');
	const anchorOf = (index: number) => page.locator('.cm-comment-anchor').filter({ hasText: quotes[index].slice(0, 20) }).first();
	const click = async (name: string, index: number, fraction: number) => {
		await scroller.evaluate((el, share) => { el.scrollTop = el.scrollHeight * share - 300; }, PARAS.findIndex((para) => para.includes(quotes[index])) / PARAS.length);
		await page.waitForTimeout(400);
		await anchorOf(index).evaluate((el, share) => {
			const scroll = el.closest('.cm-scroller')!;
			const area = scroll.getBoundingClientRect();
			scroll.scrollTop += el.getBoundingClientRect().top - (area.top + (area.height - 40) * share);
		}, fraction);
		await page.waitForTimeout(400);
		const scrolledBefore = await scroller.evaluate((el) => el.scrollTop);
		const point = await anchorOf(index).evaluate((el) => { const rect = el.getClientRects()[0]; return { x: rect.left + Math.min(40, rect.width / 2), y: rect.top + rect.height / 2 }; });
		await mark(page, point);
		await frame(page, info, `fix3-${name}-at-click`, width, theme);
		await unmark(page);
		await pointerAt(page, width, point);
		await expect(cardOf(page, width)).toBeVisible();
		await ready(page);
		const moved = Math.round(await scroller.evaluate((el) => el.scrollTop) - scrolledBefore);
		await mark(page, { x: point.x, y: point.y - moved });
		const rect = await cardOf(page, width).evaluate((el) => { const r = el.getBoundingClientRect(); return { left: Math.round(r.left), top: Math.round(r.top), right: Math.round(r.right), bottom: Math.round(r.bottom) }; });
		note(info, { item: 3, tag: TAG, frame: name, width, theme, pointer: { x: Math.round(point.x), y: Math.round(point.y) }, card: rect, scrolled: moved, viewport: page.viewportSize() });
		await frame(page, info, `fix3-${name}`, width, theme);
		await unmark(page);
		await closeCard(page);
	};
	if (phoneOf(width)) { await click('tap-middle', 1, 0.4); return; }
	await click('top', 0, 0.1);
	await click('middle', 1, 0.5);
	await click('bottom-edge', 2, 0.9);
}, { chromiumAll: true });

// ---- Fix 4: suggestions inside table cells ----
cases('fix4', async ({ page, request, info, width, theme }) => {
	const doc = await seedDoc(request, 'Trail status', TABLE_DOC);
	const reviewer = await reviewerThread(request);
	await reviewerSuggestion(request, doc, reviewer, 'In progress', 'Under way', 'Match the wording used in the board report.');
	await reviewerSuggestion(request, doc, reviewer, 'Apr 19', 'Apr 26', 'The county closed that Sunday.');
	await openDoc(page, doc, width, theme, 'Crew assignments');
	await frame(page, info, 'fix4-overview', width, theme);
	if (!AFTER && !(await page.locator('td .cm-suggestion-del').count())) {
		note(info, { item: 4, tag: TAG, cellSuggestionsDrawn: 0 });
		return;
	}
	const target = page.locator('td .cm-suggestion-del, td .cm-comment-wrap.cm-suggestion-del').filter({ hasText: 'In prog' }).first();
	await target.scrollIntoViewIfNeeded();
	await page.waitForTimeout(300);
	const point = await target.evaluate((el) => { const r = el.getClientRects()[0]; return { x: r.left + Math.min(r.width / 2, 24), y: r.top + r.height / 2 }; });
	await mark(page, point);
	await frame(page, info, 'fix4-cell-at-click', width, theme);
	await unmark(page);
	await pointerAt(page, width, point);
	await expect(cardOf(page, width)).toBeVisible();
	await ready(page);
	await mark(page, point);
	note(info, { item: 4, tag: TAG, cellSuggestionsDrawn: await page.locator('td .cm-suggestion-del').count(), card: await cardOf(page, width).evaluate((el) => { const r = el.getBoundingClientRect(); return { left: Math.round(r.left), top: Math.round(r.top), right: Math.round(r.right), bottom: Math.round(r.bottom) }; }) });
	await frame(page, info, 'fix4-cell', width, theme);
	await unmark(page);
}, { chromiumAll: true });

// ---- Fix 5: a comment whose text was deleted keeps its marker ----
cases('fix5', async ({ page, request, info, width, theme }) => {
	const doc = await seedDoc(request, 'Crew handbook', ORPHAN_DOC);
	const gone = [['Volunteers must sign the waiver at the kiosk before they pick up a tool.', 'Which waiver form? The 2026 one or the youth version?'], ['Radios are checked on the first Saturday of the month and the batteries are swapped.', 'Where do the spare batteries live?']];
	const ids: string[] = [];
	for (const [quote, body] of gone) ids.push(await personComment(request, doc, quote, body));
	await personComment(request, doc, 'First aid kits are restocked', 'Add the supplier for the kits.');
	let text = doc.text;
	for (const [quote] of gone) text = text.replace(`${quote}\n\n`, '');
	await rewrite(request, doc, text);
	await openDoc(page, doc, width, theme, 'Crew handbook');
	await expect(page.locator('.cm-content')).not.toContainText('waiver');
	await page.waitForTimeout(1200);
	await frame(page, info, 'fix5-overview', width, theme);
	const marker = page.locator(ids.map((id) => `.cm-comment-marker[data-comment-ids*="${id}"]`).join(', '));
	const allMarkers = page.locator('.cm-comment-marker');
	note(info, { item: 5, tag: TAG, markers: await allMarkers.count(), markersOfDeletedText: await marker.count(), markerIds: await allMarkers.evaluateAll((els) => els.map((el) => (el as HTMLElement).dataset.commentIds ?? '')), commentIds: ids });
	if (!(await marker.count())) {
		await page.locator('.comment-status').click();
		await expect(page.locator('[data-doc-comments-drawer], [data-adaptive-drawer] .drawer-panel').first()).toBeVisible();
		await showAllThreads(page).catch(() => undefined);
		await frame(page, info, 'fix5-list', width, theme);
		return;
	}
	await marker.first().scrollIntoViewIfNeeded();
	const box = (await marker.first().boundingBox())!;
	const point = { x: box.x + box.width / 2, y: box.y + box.height / 2 };
	await mark(page, point);
	await frame(page, info, 'fix5-marker-at-click', width, theme);
	await unmark(page);
	await pointerAt(page, width, point);
	await expect(cardOf(page, width)).toBeVisible();
	await frame(page, info, 'fix5-card', width, theme);
}, { chromiumAll: true });

// ---- Fix 6: a decided suggestion keeps its replies ----
async function decidedCard(page: Page, request: APIRequestContext, width: number, theme: Theme) {
	const doc = await seedDoc(request, 'Welcome letter', DECIDED_DOC);
	const reviewer = await reviewerThread(request);
	const id = await reviewerSuggestion(request, doc, reviewer, 'arrive by 8:30 am', 'arrive by 8:15 am', 'The briefing starts at 8:30, so people need a few minutes first.');
	await reply(request, doc, id, 'Will the kiosk be open by 8:15?');
	await openDoc(page, doc, width, theme, 'Volunteer welcome letter');
	const card = cardOf(page, width);
	const target = phoneOf(width) ? page.locator('.cm-comment-marker').first() : page.locator('.cm-suggestion-del').filter({ hasText: 'arrive by' }).first();
	await target.scrollIntoViewIfNeeded();
	await pointerAt(page, width, await target.evaluate((el) => { const r = el.getClientRects()[0]; return { x: r.left + Math.min(r.width / 2, 40), y: r.top + r.height / 2 }; }));
	await expect(card).toBeVisible();
	return { doc, id, reviewer, card };
}
cases('fix6', async ({ page, request, info, width, theme }) => {
	const { doc, id, reviewer, card } = await decidedCard(page, request, width, theme);
	await frame(page, info, 'fix6-1-suggestion-with-reply', width, theme);
	await card.getByRole('button', { name: 'Accept' }).click();
	// A decided suggestion with replies reads as decided and stays open; with the old build it turns into a settled row or closes.
	await page.waitForTimeout(1500);
	note(info, { item: 6, tag: TAG, afterAccept: { cardCount: await cardOf(page, width).count(), text: (await cardOf(page, width).allInnerTexts()).join(' | ').replace(/\s+/g, ' ').slice(0, 300) } });
	await frame(page, info, 'fix6-2-after-accept', width, theme);
	const arrived = 'Yes, the lead opens it at 8:00. Can we also say where to park?';
	const sent = await request.post('/api/delegation/docs/comments', { params, data: { artifact_id: doc.artifactId, parent_id: id, body: arrived, as_session_id: reviewer } });
	expect(sent.ok(), await sent.text()).toBeTruthy();
	await page.waitForTimeout(2500);
	note(info, { item: 6, tag: TAG, afterReply: { cardCount: await cardOf(page, width).count(), hasReply: (await cardOf(page, width).allInnerTexts()).join(' ').includes('where to park') } });
	await frame(page, info, 'fix6-3-reply-arrived', width, theme);
}, { chromiumAll: true });

// ---- Recordings (Chromium, native-rate video; a corner marker brackets the action) ----
const REC_DIR = path.join(OUT, TAG, 'recordings');
const REC_MARK = `(on) => { document.getElementById('c36-rec')?.remove(); if (!on) return; const d = document.createElement('div'); d.id = 'c36-rec'; d.setAttribute('style', 'position:fixed;left:0;top:0;width:8px;height:8px;background:#ff00ff;z-index:2147483647;pointer-events:none;'); document.body.appendChild(d); }`;
function recTake(id: string, width: 1440 | 393, body: (e: { page: Page; request: APIRequestContext; begin: () => Promise<void>; end: () => Promise<void> }) => Promise<void>) {
	const phone = phoneOf(width), size = { width, height: phone ? 852 : 900 };
	test(`rec ${id} ${width}`, async ({ browser, request }, info) => {
		test.skip(info.project.name !== 'chromium-desktop', 'Chromium only');
		test.setTimeout(150_000);
		fs.mkdirSync(REC_DIR, { recursive: true });
		const ctx = await browser.newContext({
			baseURL: (info.project.use as { baseURL?: string }).baseURL, viewport: size, colorScheme: 'light', reducedMotion: 'no-preference',
			...(phone ? { hasTouch: true, isMobile: true, deviceScaleFactor: 1 } : {}),
			recordVideo: { dir: path.join(REC_DIR, 'raw'), size },
		});
		const page = await ctx.newPage();
		let load0: number[] = [], load1: number[] = [], stampText = '';
		try {
			await body({
				page, request,
				begin: async () => { await page.evaluate(`(${REC_MARK})(true)`); load0 = os.loadavg(); },
				end: async () => { load1 = os.loadavg(); await page.evaluate(`(${REC_MARK})(false)`); },
			});
			stampText = ((await page.locator('.version-overlay').textContent()) ?? '').replace(/\s+/g, ' ').trim();
		} finally {
			const video = page.video()!;
			await ctx.close();
			const raw = path.join(REC_DIR, `${id}-${width}.webm`);
			fs.renameSync(await video.path(), raw);
			fs.appendFileSync(path.join(REC_DIR, 'metrics.jsonl'), `${JSON.stringify({ tag: TAG, id, width, stamp: stampText, loadStart: load0.map((n) => +n.toFixed(2)), loadEnd: load1.map((n) => +n.toFixed(2)) })}\n`);
		}
	});
}
for (const width of [1440, 393] as const) {
	recTake('click-open', width, async ({ page, request, begin, end }) => {
		const quote = 'rolling dip is cleared before the rains slow down, because blocked drains';
		const doc = await seedDoc(request, 'Trail plan', LONG_DOC);
		await personComment(request, doc, quote, 'Does this include the rolling dips on the Ridge Loop?');
		await openDoc(page, doc, width, 'light', 'spring work season');
		const anchor = page.locator('.cm-comment-anchor').filter({ hasText: quote.slice(0, 20) }).first();
		// CodeMirror renders only nearby lines: jump near the paragraph before looking for its anchor.
		await page.locator('.cm-scroller').evaluate((el, share) => { el.scrollTop = el.scrollHeight * share - 300; }, PARAS.findIndex((para) => para.includes(quote)) / PARAS.length);
		await page.waitForTimeout(400);
		await anchor.scrollIntoViewIfNeeded();
		await page.waitForTimeout(800);
		const point = await anchor.evaluate((el) => { const rect = el.getClientRects()[0]; return { x: rect.left + Math.min(40, rect.width / 2), y: rect.top + rect.height / 2 }; });
		await begin();
		await page.waitForTimeout(400);
		await pointerAt(page, width, point);
		await expect(cardOf(page, width)).toBeVisible();
		await page.waitForTimeout(1200);
		await end();
	});
	recTake('accept-with-replies', width, async ({ page, request, begin, end }) => {
		const { card } = await decidedCard(page, request, width, 'light');
		await page.waitForTimeout(800);
		await begin();
		await page.waitForTimeout(400);
		await card.getByRole('button', { name: 'Accept' }).click();
		await page.waitForTimeout(2500);
		await end();
	});
}
