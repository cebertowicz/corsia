import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';

import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { closeComments } from '../support/commentDrawer';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { chatComposer, createSession, loginByUrl, sendButton, sidebarThread, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { injectMessage, seedMockResponses } from '../support/mock';
import { DOC, DOC_IMAGE_NATURAL, overlay, putSheetAway, seedImageDocument } from '../support/docImageComments';
import { headerSend, holdDocumentComments, seedHeldComments } from '../support/heldComments';
import { hitBox } from '../support/hitBox';


const TAG = '@surface:doc-jump-control';
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

const READER = 'Jordan Lee';
const REPLIER = 'Maya Chen';
const THREAD_TITLE = 'Launch plan review';
const FROZEN_NOW = '2026-10-23T15:45:00.000-07:00';
const FROZEN_UTC = new Date(FROZEN_NOW).toISOString();
const LONG_NAME = 'launch-plan-for-the-Q4-onboarding-and-support-readiness-review.md';
let seconds = 0;

const SECTIONS = Array.from({ length: 12 }, (_, i) => ({
	heading: `Workstream ${i + 1}`,
	anchor: `Workstream ${i + 1} needs one named owner before the freeze.`,
	ask: `Who owns workstream ${i + 1}?`,
	reply: i % 3 === 0
		? `Owner confirmed for workstream ${i + 1}. I added the handover notes and the escalation path to the appendix so nobody has to ask twice, and I will raise any gap in the Thursday sync before it becomes a problem.`
		: `Owner confirmed for workstream ${i + 1}.`,
}));
const TEXT = [
	'# Q4 launch plan', '',
	...SECTIONS.flatMap((section) => [
		`## ${section.heading}`, '',
		`This workstream covers the first-run screens, the guided setup and the help pages for the Q4 launch. ${section.anchor} Status is reported in the Thursday sync, and any slip is raised the same day.`, '',
		'Cut the time from install to first useful result to under ten minutes for a new person, and keep the support queue flat through launch week. Land the announcement with every channel on the same day.', '',
	]),
].join('\n');

const CHAT = [
	{ role: 'user', content: 'Can you draft the Q4 launch plan for the onboarding flow?' },
	{ role: 'assistant', content: 'I drafted launch-plan.md with one section per workstream. Each needs a named owner, so I left a question on every one.', metadata: { for_user: true } },
] as Array<{ role: string; content: string; metadata?: Record<string, unknown> }>;

const IMAGE_THREAD_TITLE = 'Walkthrough review';

// One backend serves every test in the run and outlives each of them, so a fixed session title means the
// sidebar carries several rows with the same words by the later tests: `sidebarThread`'s exact getByText then
// hits strict mode, and a `.first()` on a title opens whichever row the list happens to put first, which is
// another test's document. Each test titles its own session, and every place that opens a thread by title
// uses that test's title, so a collision fails visibly instead of reading somebody else's state.
const RUN = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
let titleSeq = 0;
let threadTitle = '';
let imageThreadTitle = '';
test.beforeEach(() => {
	titleSeq += 1;
	threadTitle = `${THREAD_TITLE} ${RUN}-${titleSeq}`;
	imageThreadTitle = `${IMAGE_THREAD_TITLE} ${RUN}-${titleSeq}`;
});
const IMAGE_CHAT = [
	{ role: 'user', content: 'Can you check the header and the save button on the new walkthrough screen?' },
	{ role: 'assistant', content: 'I looked at both and answered on the pins in walkthrough.md.', metadata: { for_user: true } },
] as typeof CHAT;

function skipMismatchedProject(width: number, projectName: string): void {
	test.skip((width === 393) !== projectName.endsWith('mobile'), 'Each device project captures its native requested viewport.');
}

async function freezeClock(page: Page): Promise<void> {
	await page.addInitScript((now) => {
		const RealDate = Date;
		class FrozenDate extends RealDate {
			constructor(...args: ConstructorParameters<DateConstructor>) { super(...(args.length ? args : [now])); }
			static now() { return new RealDate(now).valueOf(); }
		}
		Object.setPrototypeOf(FrozenDate, RealDate);
		window.Date = FrozenDate as DateConstructor;
		// A reply counts read after a 1.5 s dwell. While a frame is being taken the dwell is held, so the frame shows the reply
		// still marked New however long the capture takes; releasing the hold lets the dwell finish within 100 ms.
		const realSetTimeout = window.setTimeout.bind(window);
		(window as unknown as { __holdDwell: boolean }).__holdDwell = false;
		window.setTimeout = ((fn: TimerHandler, delay?: number, ...args: unknown[]) => {
			if (delay !== 1500 || typeof fn !== 'function') return realSetTimeout(fn, delay, ...args);
			const run = (): number | void => (window as unknown as { __holdDwell: boolean }).__holdDwell ? realSetTimeout(run, 100) : fn(...args);
			return realSetTimeout(run, delay);
		}) as typeof window.setTimeout;
	}, FROZEN_NOW);
}

async function holdDwell(page: Page, hold: boolean): Promise<void> {
	await page.evaluate((value) => { (window as unknown as { __holdDwell: boolean }).__holdDwell = value; }, hold);
}


async function startMotionProbe(page: Page): Promise<void> {
	await page.evaluate(() => {
		type Probe = { events: unknown[]; cleanup: () => void };
		const w = window as unknown as { __replyMotion?: Probe };
		w.__replyMotion?.cleanup();
		const events: unknown[] = [];
		const selector = '[data-doc-jump], .drawer-motion, .drawer-backdrop:not(.outro-shield), [data-comment-id]';
		const relevant = (node: Element) => node.matches(selector) || !!node.closest('[data-doc-jump], .drawer-motion');
		const details = (animation: Animation) => {
			const effect = animation.effect as KeyframeEffect | null;
			return { timing: effect?.getTiming(), keyframes: effect?.getKeyframes() };
		};
		const listener = (event: Event) => {
			const node = event.target;
			if (!(node instanceof Element) || !relevant(node)) return;
			events.push({ type: event.type, property: event instanceof TransitionEvent ? event.propertyName : event instanceof AnimationEvent ? event.animationName : null, target: node.className, animations: node.getAnimations().map(details) });
		};
		document.addEventListener('transitionrun', listener, true);
		document.addEventListener('animationstart', listener, true);
		// Svelte also starts transitions via Web Animations, which emits neither DOM event.
		const original = Element.prototype.animate;
		Element.prototype.animate = function (...args: Parameters<typeof original>) {
			const animation = original.apply(this, args);
			if (relevant(this)) events.push({ type: 'web-animation-created', property: null, target: this.className, animations: [details(animation)] });
			return animation;
		};
		w.__replyMotion = { events, cleanup: () => {
			document.removeEventListener('transitionrun', listener, true);
			document.removeEventListener('animationstart', listener, true);
			Element.prototype.animate = original;
		} };
	});
}

async function saveMotionProbe(page: Page): Promise<Array<{ target: string; animations: Array<{ timing: { duration: number }; keyframes: Array<{ transform?: string; opacity?: string }> }> }>> {
	await page.waitForTimeout(600);
	const events = await page.evaluate(() => {
		const probe = (window as unknown as { __replyMotion: { events: unknown[]; cleanup: () => void } }).__replyMotion;
		probe.cleanup();
		return probe.events;
	});

	return events as Array<{ target: string; animations: Array<{ timing: { duration: number }; keyframes: Array<{ transform?: string; opacity?: string }> }> }>;
}

function sqlite(statements: Array<[string, Array<string | number | null>]>): void {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !stateRoot) throw new Error('Requires isolated synthetic state');
	const start = seconds;
	seconds += statements.flatMap(([, params]) => params).filter((value) => value === null).length;
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', [
			'import json, sqlite3, sys',
			'from datetime import datetime, timedelta',
			'connection = sqlite3.connect(sys.argv[1])',
			'base = datetime.fromisoformat(sys.argv[3].replace("Z", "+00:00"))',
			'offset = int(sys.argv[4])',
			'index = 0',
			'for sql, params in json.loads(sys.argv[2]):',
			'    row = []',
			'    for value in params:',
			'        if value is None:',
			'            value = (base + timedelta(seconds=offset + index)).isoformat()',
			'            index += 1',
			'        row.append(value)',
			'    connection.execute(sql, row)',
			'connection.commit()',
			'connection.close()',
		].join('\n'), path.join(stateRoot, 'corsia.db'), JSON.stringify(statements), FROZEN_UTC, String(start),
	], { cwd: repoRoot, encoding: 'utf8' });
}

function addAgentReply(artifactId: string, rootId: string, threadId: string, body: string, author = REPLIER): void {
	sqlite([[
		"INSERT INTO doc_comments (id, artifact_id, parent_id, author, author_kind, author_thread_id, body, created_at, updated_at) VALUES (lower(hex(randomblob(16))), ?, ?, ?, 'agent', ?, ?, ?, ?)",
		[artifactId, rootId, author, threadId, body, null, null],
	]]);
}

function tidy(artifactId: string): void {
	sqlite([
		["UPDATE doc_comments SET author=? WHERE author='e2e'", [READER]],
		['UPDATE doc_comments SET created_at=?, updated_at=? WHERE artifact_id=? AND parent_id IS NULL', [FROZEN_UTC, FROZEN_UTC, artifactId]],
		["UPDATE models SET display_name='Claude Sonnet 5.5' WHERE id='mock-test-model'", []],
		["UPDATE plan_nodes SET state='closed', archived_at=? WHERE title LIKE '% left % comment%'", [null]],
	]);
}

async function onlyThread(request: APIRequestContext, keepId: string): Promise<void> {
	const listed = await (await request.get('/api/threads', { params: { token: TEST_TOKEN } })).json();
	for (const thread of (listed.threads ?? []) as Array<{ id: string }>) {
		if (thread.id !== keepId) await request.delete(`/api/threads/${thread.id}`, { params: { token: TEST_TOKEN } });
	}
}

/** A long document of twelve threads, each asked by the reader; `replied` are answered by someone else. */
async function seedTextDocument(
	page: Page, request: APIRequestContext, threadId: string, folder: string, name: string, replied: readonly number[],
): Promise<{ artifactId: string; docPath: string; roots: string[] }> {
	seconds = 0;
	await freezeClock(page);
	const tree = await request.get('/api/files/tree', { params: { token: TEST_TOKEN } });
	const workspace = ((await tree.json()).roots as Array<{ kind: string; path: string }>).find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const docPath = path.join(workspace!, folder, name);
	mkdirSync(path.dirname(docPath), { recursive: true });
	const saved = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: docPath, text: TEXT, thread_id: threadId, create: true } });
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	const roots: string[] = [];
	for (const section of SECTIONS) {
		const from = TEXT.indexOf(section.anchor);
		expect(from).toBeGreaterThan(-1);
		const root = await request.post(`/api/artifacts/${artifactId}/comments`, {
			params: { token: TEST_TOKEN },
			data: { body: section.ask, anchor_text: section.anchor, from_off: from, to_off: from + section.anchor.length, delivery_policy: 'immediate' },
		});
		expect(root.ok(), await root.text()).toBeTruthy();
		roots.push((await root.json()).comment.id as string);
	}
	tidy(artifactId);
	for (const index of replied) addAgentReply(artifactId, roots[index], threadId, SECTIONS[index].reply);
	for (const message of CHAT) await injectMessage(request, threadId, message, TEST_TOKEN);
	sqlite([['UPDATE messages SET created_at=? WHERE thread_id=?', [FROZEN_UTC, threadId]]]);
	return { artifactId, docPath, roots };
}

async function stageWorkspace(page: Page, threadId: string, docPath: string, name: string, surface: 'artifact' | 'chat'): Promise<void> {
	await page.addInitScript(({ threadId, docPath, name, surface }) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
		threads: { [threadId]: { tabs: [{ instanceId: 'jump-control', artifactId: null, path: docPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'jump-control', visible: true, activeSurface: surface } },
		splitWidth: 420,
	})), { threadId, docPath, name, surface });
}

async function openThread(page: Page, title: string): Promise<void> {
	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, title).click();
}

async function reloadComments(page: Page, artifactId: string): Promise<void> {
	await page.evaluate(async (id) => {
		const { loadComments } = await import('/src/lib/stores/docComments.ts');
		await loadComments(id);
	}, artifactId);
}

// Back, not Escape: Escape first closes the pin's own card in the viewer, and Back leaves the viewer in one step.
async function backToDocument(page: Page): Promise<void> {
	// The reply counts read once its thread has been on screen for 1.5 s; leaving sooner leaves it waiting, which is the product's rule.
	await page.waitForTimeout(1700);
	await putSheetAway(page);
	await page.getByRole('button', { name: 'Back to walkthrough.md', exact: true }).click();
	await expect(overlay(page)).toHaveCount(0);
}

const controlButton = (page: Page) => page.locator('[data-doc-jump]').getByRole('button');
const controlBox = (page: Page) => page.locator('[data-doc-jump]');

/** Arriving on a thread opens it, so its New replies clear once its card is shown. */
async function awaitFocusedRead(page: Page): Promise<void> {
	await expect(page.locator('[data-comment-id].focused')).toBeVisible();
	await expect(page.locator('[data-comment-id].focused .ct-reply.new')).toHaveCount(0);
}

/** The control's face is its touch target: like chat's control, no pseudo-element padding. */
async function controlFace(page: Page) {
	return await controlButton(page).boundingBox() as { x: number; y: number; width: number; height: number };
}

/** Its height is the touch target under a finger and chat's control size under a mouse. */
const minControlHeight = (width: number) => (width === 393 ? 44 : 36);

/** Where the jump actually landed, in the region the reader can see: the thread's own card, or the text it marks,
 * whichever is painted. A zero-area node — the gutter rail line a thread owns — is not something a reader sees, so it
 * never answers this, and the pane is the measured node's own scroll container, since the card and the inline text live
 * in different ones. What is measured is the node's TEXT, by Range: a card's padding is not something the reader came
 * to read, and the drawer parks a focused thread against the pane's edge (`CommentThread.svelte` scrolls with
 * `block: 'nearest'`), which cuts the card's box by its own padding without hiding a word. `whole` is the verdict:
 * wholly inside, or taller than the pane with its top inside. */
async function landedBox(page: Page, id: string): Promise<{ from: string; top: number; bottom: number; paneTop: number; paneBottom: number; whole: boolean; roomBelow: number } | null> {
	return page.evaluate((wanted) => {
		const cards = [...document.querySelectorAll<HTMLElement>(`[data-comment-id="${wanted}"]`)];
		const marks = [...document.querySelectorAll<HTMLElement>('[data-comment-ids]')].filter((node) => (node.dataset.commentIds ?? '').split(',').includes(wanted));
		for (const node of [...cards, ...marks]) {
			const range = document.createRange();
			range.selectNodeContents(node);
			const rect = range.getBoundingClientRect();
			if (rect.width < 1 || rect.height < 1) continue;
			let host: HTMLElement | null = node.parentElement;
			while (host && !/(auto|scroll)/.test(getComputedStyle(host).overflowY)) host = host.parentElement;
			if (!host) continue;
			const pane = host.getBoundingClientRect();
			const fits = rect.top >= pane.top - 0.5 && (rect.bottom <= pane.bottom + 0.5 || rect.height >= pane.height);
			return { from: `${node.tagName}.${String(node.className).slice(0, 32)}`, top: Math.round(rect.top), bottom: Math.round(rect.bottom), paneTop: Math.round(pane.top), paneBottom: Math.round(pane.bottom), whole: fits, roomBelow: Math.round(host.scrollHeight - host.clientHeight - host.scrollTop) };
		}
		return null;
	}, id);
}

/** The line the reader came to read: the document's own line box that holds a thread's anchor text, against the pane
 * that holds it. A thread's inline marks (`cm-comment-anchor`) carry no thread id and CodeMirror splits them wherever
 * the text breaks, so the line is the smallest thing that can honestly be named for a thread here. */
async function anchorBox(page: Page, anchorText: string): Promise<{ from: string; top: number; bottom: number; paneTop: number; paneBottom: number; whole: boolean } | null> {
	return page.evaluate((wanted) => {
		const node = [...document.querySelectorAll<HTMLElement>('.cm-line')].find((el) => (el.textContent ?? '').includes(wanted));
		if (!node) return null;
		const range = document.createRange();
		range.selectNodeContents(node);
		const rect = range.getBoundingClientRect();
		let host: HTMLElement | null = node.parentElement;
		while (host && !/(auto|scroll)/.test(getComputedStyle(host).overflowY)) host = host.parentElement;
		if (!host) return null;
		const pane = host.getBoundingClientRect();
		return { from: 'DIV.cm-line', top: Math.round(rect.top), bottom: Math.round(rect.bottom), paneTop: Math.round(pane.top), paneBottom: Math.round(pane.bottom), whole: rect.top >= pane.top - 0.5 && rect.bottom <= pane.bottom + 0.5 };
	}, anchorText);
}

/** Where the design's states are photographed: outside the e2e script's own scratch tree, so they survive its cleanup. */
function stillPath(width: number, theme: string, state: string): string {
	const dir = process.env.DOC_JUMP_STILL_DIR ?? path.join(repoRoot, '.qa', 'jump-stills');
	mkdirSync(dir, { recursive: true });
	return path.join(dir, `jump-${width}-${theme}-${state}.png`);
}

/** The count, once drawn: what it says, and that nothing else on the document's surface sits under it. */
async function expectControl(page: Page, text: string): Promise<void> {
	await expect(controlButton(page)).toHaveText(new RegExp(text));
	const view = page.viewportSize()!;
	// Opening the artifact on mobile can still be moving its whole surface when its text appears.
	await expect.poll(async () => {
		const control = await controlBox(page).boundingBox();
		return !!control && control.x >= 0 && control.x + control.width <= view.width + 0.5 && control.y + control.height <= view.height + 0.5;
	}, { message: 'the settled control is inside the viewport' }).toBe(true);
}

/** What is still unread, read from the store: the control's own face is not proof that a thread is unread, since it steps aside when the text is on screen. */
async function unreadCountInStore(page: Page, artifactId: string): Promise<number> {
	return await page.evaluate(async (id) => {
		const { docComments } = await import('/src/lib/stores/docComments.ts');
		const { unreadCommentCount } = await import('/src/lib/docUnread.ts');
		let byArtifact: Record<string, unknown> = {};
		(docComments as { subscribe: (fn: (value: Record<string, unknown>) => () => void) => void }).subscribe((value) => { byArtifact = value; return () => undefined; })();
		return unreadCommentCount((byArtifact[id] ?? []) as never);
	}, artifactId);
}

/** What the control points at is named in its accessible name, never in the label it shows; the arrow carries the side. */
async function expectControlPoints(page: Page, count: number, direction: 'above' | 'below'): Promise<void> {
	await expect(controlButton(page)).toHaveAccessibleName(new RegExp(`^${count} new, next ${direction}: .+$`));
	await expect(controlButton(page)).toContainText(direction === 'above' ? '↑' : '↓');
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		const size = { width, height: width === 393 ? 852 : 900 };

		test(`${TAG} ${theme} ${width}px: J1 a reply below the fold is announced and the control jumps to it`, async ({ page, request }, testInfo) => {
			test.setTimeout(90_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize(size);
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, threadTitle);
			await onlyThread(request, threadId);
			const doc = await seedTextDocument(page, request, threadId, `j1-${theme}-${width}`, 'launch-plan.md', [8, 10]);
			const heldFrom = TEXT.indexOf(SECTIONS[0].anchor);
			await holdDocumentComments(request);
			await seedHeldComments(request, doc.artifactId, 1, 'Review', { text: SECTIONS[0].anchor, from: heldFrom, to: heldFrom + SECTIONS[0].anchor.length });
			tidy(doc.artifactId);
			await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
			await openThread(page, threadTitle);
			await expect(page.locator('.doc .cm-content')).toContainText('Q4 launch plan');
			// Opened at the top: both replies are below the fold, so the bottom band offers the jump once.
			await expectControl(page, '2 new');
			await expect(headerSend(page)).toBeVisible();
			const sendBox = await hitBox(headerSend(page));
			const controlBounds = await controlFace(page);
			expect(controlBounds.height, 'the jump target is at least its touch target high').toBeGreaterThanOrEqual(minControlHeight(width));
			expect(controlBounds.width, 'the jump target is at least 44px wide').toBeGreaterThanOrEqual(44);
			expect(controlBounds.x < sendBox.x + sendBox.width && sendBox.x < controlBounds.x + controlBounds.width && controlBounds.y < sendBox.y + sendBox.height && sendBox.y < controlBounds.y + controlBounds.height, 'held-comment Send stays clear of the jump control').toBe(false);

			// The label is the count alone; the thread it points at is named in the accessible name, and it is the first reply below the fold.
			await expect(controlButton(page)).toHaveText(/^[↑↓]\d+ new$/);
			await expectControlPoints(page, 2, 'below');
			// The control's own face answers a tap at either vertical edge.
			const edgeHits = await controlButton(page).evaluate((button) => {
				const box = button.getBoundingClientRect();
				const x = box.left + box.width / 2;
				const center = box.top + box.height / 2;
				return [box.height / 2 - 3, -(box.height / 2 - 3)].map((offset) => button.contains(document.elementFromPoint(x, center + offset)));
			});
			expect(edgeHits).toEqual([true, true]);
			const anchor = page.locator('.cm-comment-anchor').first();
			await anchor.scrollIntoViewIfNeeded();
			const anchorBox = (await anchor.boundingBox())!;
			const editorBox = (await page.locator('.doc-main').boundingBox())!;
			const noticeBounds = (await controlBox(page).boundingBox())!;
			expect(anchorBox.y).toBeGreaterThanOrEqual(editorBox.y - 0.5);
			expect(anchorBox.y + anchorBox.height).toBeLessThanOrEqual(editorBox.y + editorBox.height + 0.5);
			expect(anchorBox.y + anchorBox.height).toBeLessThanOrEqual(noticeBounds.y);

			// Move the editing caret to the end: the last line must remain visible above the notice.
			await page.locator('.doc .cm-content').click();
			await page.keyboard.press('ControlOrMeta+End');
			await page.keyboard.press('ArrowLeft');
			const caretBox = await page.locator('.doc .cm-content').evaluate((editor) => {
				const selection = window.getSelection();
				if (!selection?.isCollapsed || !selection.anchorNode || !editor.contains(selection.anchorNode)) throw new Error('Editor caret is not focused');
				const rect = selection.getRangeAt(0).getBoundingClientRect();
				return { x: rect.x, y: rect.y, height: rect.height };
			});
			expect(caretBox.height, 'caret has visible geometry').toBeGreaterThan(0);
			const editorAfter = (await page.locator('.doc-main').boundingBox())!;
			const bandAfter = (await controlBox(page).boundingBox())!;
			expect(caretBox.y + caretBox.height, 'caret stays inside the editor').toBeLessThanOrEqual(editorAfter.y + editorAfter.height + 0.5);
			expect(caretBox.y + caretBox.height, 'caret stays above the bottom band').toBeLessThanOrEqual(bandAfter.y);
			await expect(page.locator('.doc .cm-content')).toBeFocused();
			await page.locator('.cm-scroller').evaluate((el) => { el.scrollTop = 0; });
			const before = await page.locator('.cm-scroller').evaluate((el) => el.scrollTop);
			await holdDwell(page, true);
			await controlButton(page).click();
			// The waiting reply it pointed at is brought into view, focused, and the comments list opens at it.
			await expect(page.locator(`[data-comment-id="${doc.roots[8]}"]`)).toHaveClass(/focused/);
			await expect.poll(() => page.locator('.cm-scroller').evaluate((el) => el.scrollTop)).toBeGreaterThan(before);
			let steadyInside = 0;
			// Where it lands matters as much as that it moved: the whole landed line sits inside the pane the reader can see,
			// clear of its top edge, and stays there once the drawer has taken its width from the pane.
			await expect.poll(async () => {
				const landed = await landedBox(page, doc.roots[8]);
				console.log('LANDED', JSON.stringify(landed));
				const fits = landed?.whole === true;
				steadyInside = fits ? steadyInside + 1 : 0;
				return steadyInside >= 2;
			}, { message: 'the jumped-to line sits wholly inside the visible pane and stays there' }).toBe(true);
			// The arrival reads that thread, and the one still waiting has its own line on screen: with nothing off-screen the control steps aside while the count still says one.
			await expect.poll(() => unreadCountInStore(page, doc.artifactId)).toBe(1);
			await expect(controlBox(page)).toHaveCount(0);

			const input = page.locator(`[data-comment-id="${doc.roots[8]}"] textarea[placeholder="Reply…"]`);
			await input.fill('Thanks, I will add the handover notes.');
			await input.focus();
			await expect(input).toBeFocused();
			await expect(controlBox(page)).toHaveCount(0);
			await holdDwell(page, false);
		});
	}
}

for (const width of [1440, 393]) {
	test(`${TAG} extremes ${width}px: long reply and large count`, async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		skipMismatchedProject(width, testInfo.project.name);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		const threadId = await createSession(request, threadTitle);
		await onlyThread(request, threadId);
		const doc = await seedTextDocument(page, request, threadId, `extremes-${width}`, 'launch-plan.md', []);
		const token = 'launchreadiness'.repeat(20);
		addAgentReply(doc.artifactId, doc.roots[8], threadId, token);
		await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
		await openThread(page, threadTitle);
		await expectControl(page, '1 new');
		await holdDwell(page, true);
		await controlButton(page).click();
		await expect(page.locator(`[data-comment-id="${doc.roots[8]}"]`)).toHaveClass(/focused/);
		await expect(page.getByText(token)).toBeVisible();

		// Leave the comments with the keyboard, which this spec's own focus legs prove at both widths: while a sheet is
		// up over the text the band is right to stay away, so the journey has to be out of it before it can ask what the
		// band says. Measured on the Close button instead: at 1440 it steps back to the sheet with `[role=dialog]` and
		// the backdrop still on screen, at 393 it closes the comments, and a keystroke sent after that closes them lands
		// on the workbench and takes the document away with it. Escape, pressed while the sheet still has focus, leaves
		// the same surface at either width and hands the document the focus back.
		await page.keyboard.press('Escape');
		await expect(page.locator('[role="dialog"]')).toHaveCount(0);
		await expect(page.locator('.doc .cm-content')).toBeFocused();
		const statement = "INSERT INTO doc_comments (id, artifact_id, parent_id, author, author_kind, author_thread_id, body, created_at, updated_at) VALUES (lower(hex(randomblob(16))), ?, ?, ?, 'agent', ?, ?, ?, ?)";
		// The control's own arrival read the long reply; 120 more on another thread make the large count.
		sqlite(Array.from({ length: 120 }, (_, index) => [statement, [doc.artifactId, doc.roots[10], REPLIER, threadId, `Update ${index + 1}.`, null, null]]));
		await reloadComments(page, doc.artifactId);
		// Back to the top, so those 120 sit on a thread below the fold again and there is something to point at.
		await page.locator('.cm-scroller').evaluate((el) => { el.scrollTop = 0; });
		await expectControl(page, '120 new');

	});
}

for (const width of [1440, 393]) {
	test(`${TAG} viewed failure and partial response ${width}px`, async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		skipMismatchedProject(width, testInfo.project.name);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		const threadId = await createSession(request, threadTitle);
		await onlyThread(request, threadId);
		const doc = await seedTextDocument(page, request, threadId, `viewed-partial-${width}`, 'launch-plan.md', [8, 10]);
		await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
		await openThread(page, threadTitle);
		await expectControl(page, '2 new');
		const viewedRoute = `**/api/artifacts/${doc.artifactId}/comments/${doc.roots[8]}/viewed`;
		await page.route(viewedRoute, route => route.fulfill({ status: 500, body: 'Unavailable' }));
		const failed = await page.evaluate(async ({ artifactId, rootId }) => {
			const { docComments, markThreadViewed } = await import('/src/lib/stores/docComments.ts');
			let replyId: string | undefined;
			const off = docComments.subscribe(all => { replyId = all[artifactId]?.find(row => row.id === rootId)?.newReplyIds?.at(-1); });
			off();
			if (!replyId) throw new Error('No unread reply');
			try { await markThreadViewed(artifactId, rootId, replyId); return false; } catch { return true; }
		}, { artifactId: doc.artifactId, rootId: doc.roots[8] });
		expect(failed).toBe(true);
		await expectControl(page, '2 new');

		await page.unroute(viewedRoute);
		const commentsRoute = `**/api/artifacts/${doc.artifactId}/comments?*`;
		await page.route(commentsRoute, async route => {
			const response = await route.fetch();
			const body = await response.json();
			body.comments = body.comments.filter((row: { id: string }) => row.id === doc.roots[8]);
			expect(body.comments).toHaveLength(1);
			await route.fulfill({ response, json: body });
		});
		await reloadComments(page, doc.artifactId);
		await expectControl(page, '1 new');

		await page.unroute(commentsRoute);
	});

	test(`${TAG} fetch failure ${width}px: retain the last reply count`, async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		skipMismatchedProject(width, testInfo.project.name);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		const threadId = await createSession(request, threadTitle);
		await onlyThread(request, threadId);
		const doc = await seedTextDocument(page, request, threadId, `fetch-failure-${width}`, 'launch-plan.md', [8, 10]);
		await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
		await openThread(page, threadTitle);
		await expectControl(page, '2 new');
		const route = `**/api/artifacts/${doc.artifactId}/comments?*`;
		for (const failure of ['server', 'permission', 'offline'] as const) {
			await page.route(route, (requestRoute) => failure === 'offline'
				? requestRoute.abort('internetdisconnected')
				: requestRoute.fulfill({ status: failure === 'permission' ? 403 : 500, body: 'Unavailable' }));
			const loadState = await page.evaluate(async (id) => {
				const { commentLoads, loadComments } = await import('/src/lib/stores/docComments.ts');
				await loadComments(id).catch(() => undefined);
				let state: string | undefined;
				const unsubscribe = commentLoads.subscribe((all) => { state = all[id]?.state; });
				unsubscribe();
				return state;
			}, doc.artifactId);
			expect(loadState).toBe('error');
			await expectControl(page, '2 new');

			await page.unroute(route);
			await reloadComments(page, doc.artifactId);
			await expectControl(page, '2 new');
		}
	});
}

for (const width of [1440, 393]) {
	test(`${TAG} light ${width}px: activity and reply notices stay clear`, async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		skipMismatchedProject(width, testInfo.project.name);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		const threadId = await createSession(request, threadTitle);
		await onlyThread(request, threadId);
		const doc = await seedTextDocument(page, request, threadId, `activity-${width}`, 'launch-plan.md', [8, 10]);
		const reviewerId = await createSession(request, 'Review colleague');
		await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
		await openThread(page, threadTitle);
		await expectControl(page, '2 new');
		// Activity is emitted by the settled tool call, not by the direct comment API.
		const reviewer = await page.context().newPage();
		await loginByUrl(reviewer);
		if (width === 393) await reviewer.getByRole('button', { name: 'Toggle sidebar' }).click();
		await sidebarThread(reviewer, 'Review colleague').click();
		await waitForWsConnected(reviewer);
		await seedMockResponses(request, [
			{ tool_calls: [{ id: `activity-${width}`, name: 'doc_comment_create', arguments: JSON.stringify({ artifact_id: doc.artifactId, quote: SECTIONS[11].anchor, body: 'Please review this final workstream.' }) }], finish_reason: 'tool_calls', match_thread: reviewerId },
			{ content: 'Review recorded.', finish_reason: 'stop', match_thread: reviewerId },
		]);
		await chatComposer(reviewer).fill('Review the final workstream.');
		await sendButton(reviewer).click();
		const activity = page.locator('[data-doc-activity-pill]');
		await expect(activity).toBeVisible();
		{
			const activityBox = await hitBox(activity.getByRole('button'));
			const replyBox = await controlFace(page);
			expect(replyBox.height, 'the jump control meets its touch target height').toBeGreaterThanOrEqual(minControlHeight(width));
			expect(replyBox.x < activityBox.x + activityBox.width && activityBox.x < replyBox.x + replyBox.width && replyBox.y < activityBox.y + activityBox.height && activityBox.y < replyBox.y + replyBox.height, 'activity stays clear of the reply target').toBe(false);
		}
		if (width === 393) {
			await expect.poll(async () => {
				const editor = await page.locator('.doc .ed-wrap').boundingBox();
				const pill = await activity.boundingBox();
				return !!editor && !!pill && editor.y + editor.height <= pill.y + 0.5;
			}, { message: 'mobile editor text clears the below activity notice' }).toBe(true);
		}

		{
			const scroller = page.locator('.doc .cm-scroller');
			const before = await scroller.evaluate((node) => node.scrollTop);
			await activity.getByRole('button').click();
			await expect.poll(() => scroller.evaluate((node) => node.scrollTop)).toBeGreaterThan(before);
		}
		await reviewer.close();
	});
}

for (const width of [1440, 393]) {
	for (const key of ['Enter', 'Space'] as const) {
		test(`${TAG} keyboard ${width}px ${key}: tab to the notice and return from comments`, async ({ page, request }, testInfo) => {
			test.setTimeout(90_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			const threadId = await createSession(request, threadTitle);
			await onlyThread(request, threadId);
			const doc = await seedTextDocument(page, request, threadId, `keyboard-${width}-${key}`, 'launch-plan.md', [8, 10]);
			await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
			await openThread(page, threadTitle);
			await expectControl(page, '2 new');
			await page.locator('body').focus();
			let reached = false;
			for (let i = 0; i < 100; i += 1) {
				await page.keyboard.press('Tab');
				if (await controlButton(page).evaluate((button) => button === document.activeElement)) { reached = true; break; }
			}
			expect(reached, 'notice is reachable in the tab order').toBe(true);
			await page.keyboard.press('Shift+Tab');
			await expect(controlButton(page)).not.toBeFocused();
			await page.keyboard.press('Tab');
			await expect(controlButton(page)).toBeFocused();

			await holdDwell(page, true);
			await page.keyboard.press(key);
			await expect(page.locator(`[data-comment-id="${doc.roots[8]}"]`)).toHaveClass(/focused/);
			await expect(page.locator('[data-doc-comments-drawer]')).toBeVisible();
			await expect.poll(() => page.locator('[data-doc-comments-drawer]').evaluate((drawer) => drawer.contains(document.activeElement) || !!drawer.closest('[role="dialog"]')?.contains(document.activeElement))).toBe(true);

			await closeComments(page);
			await expect(page.locator('[data-doc-comments-drawer]')).toHaveCount(0);
			// The drawer takes focus back to the document itself: the thread it jumped to is read and on screen, so the band
			// has nothing left to point at. The reader going back up makes the last thread a target, and the notice is drawn again.
			await expect(page.locator('.doc .cm-content')).toBeFocused();
			// Out of the editor first: a caret in the text keeps its own line in view and would hold the pane at the landing.
			await page.locator('body').focus();
			await page.locator('.cm-scroller').evaluate((el) => { el.scrollTop = 0; });
			await expectControl(page, '1 new');
			await expectControlPoints(page, 1, 'below');
			await holdDwell(page, false);
		});
	}
}

for (const width of [1440, 393]) {
	for (const route of ['Escape', 'scrim'] as const) {
		for (const reduced of [false, true]) {
			test(`${TAG} focus ${width}px ${route} ${reduced ? 'reduced' : 'standard'}: return to the reply notice`, async ({ page, request }, testInfo) => {
				test.setTimeout(90_000);
				test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
				skipMismatchedProject(width, testInfo.project.name);
				await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
				await page.emulateMedia({ reducedMotion: reduced ? 'reduce' : 'no-preference' });
				const threadId = await createSession(request, threadTitle);
				await onlyThread(request, threadId);
				const doc = await seedTextDocument(page, request, threadId, `focus-${width}-${route}-${reduced}`, 'launch-plan.md', [8, 10]);
				await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
				await openThread(page, threadTitle);
				await expectControl(page, '2 new');
				await holdDwell(page, true);
				await controlButton(page).focus();
				await page.keyboard.press('Enter');
				const backdrop = page.locator('[data-adaptive-drawer].drawer-backdrop');
				await expect(backdrop).toBeVisible();
				await startMotionProbe(page);
				if (route === 'Escape') await page.keyboard.press('Escape');
				else {
				// The strip of document above the sheet is what a scrim tap has to hit: at 393 the point mid-viewport is inside
				// the sheet itself, so it opens nothing and closes nothing.
					const back = (await backdrop.boundingBox())!;
					const panel = await page.locator('.drawer-panel').first().boundingBox();
					await page.mouse.click(back.x + 12, Math.max(back.y + 12, (panel?.y ?? back.y + 40) - 20));
				}
				await expect(backdrop).toHaveCount(0);
				await expect(page.locator('.doc .cm-content')).toBeFocused();
				// Out of the editor first: a caret in the text keeps its own line in view and would hold the pane at the landing.
				await page.locator('body').focus();
				await page.locator('.cm-scroller').evaluate((el) => { el.scrollTop = 0; });
				await expectControl(page, '1 new');
				await saveMotionProbe(page);
				await holdDwell(page, false);
			});
		}
	}
}

for (const width of [1440, 393]) {
	test(`${TAG} modal ${width}px: reply notice stays unavailable through sheet outro`, async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		skipMismatchedProject(width, testInfo.project.name);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		const threadId = await createSession(request, threadTitle);
		await onlyThread(request, threadId);
		const doc = await seedTextDocument(page, request, threadId, `modal-${width}`, 'launch-plan.md', [8, 10]);
		await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
		await openThread(page, threadTitle);
		await expectControl(page, '2 new');
		const box = (await controlButton(page).boundingBox())!;
		await holdDwell(page, true);
		await controlButton(page).click();
		const sheet = page.locator('[data-adaptive-drawer].drawer-backdrop');
		await expect(sheet).toBeVisible();
		await expect(controlBox(page)).toHaveCount(0);
		const x = box.x + box.width / 2, y = box.y + box.height / 2;
		expect(await page.evaluate(({ x, y }) => !!document.elementFromPoint(x, y)?.closest('[data-doc-jump]'), { x, y }), 'the control cannot be hit while the sheet is up').toBe(false);
		await closeComments(page);
		await expect(sheet.first()).toBeVisible();
		await expect(controlBox(page)).toHaveCount(0);
		expect(await page.evaluate(({ x, y }) => !!document.elementFromPoint(x, y)?.closest('[data-doc-jump]'), { x, y }), 'the control cannot be hit while the sheet is up').toBe(false);
		await expect(sheet).toHaveCount(0);
		// Arriving from the chat's control opened the first thread, so only the other one is still new - and its line is the one
		// the reader is standing on, so the notice steps aside while the count keeps saying one.
		await expect.poll(() => unreadCountInStore(page, doc.artifactId)).toBe(1);
		// Send the reader back up: the remaining thread's line is out of view again, and the notice is drawn with its count.
		await page.locator('body').focus();
		await page.locator('.cm-scroller').evaluate((el) => { el.scrollTop = 0; });
		await expectControl(page, '1 new');
		await holdDwell(page, false);
	});
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		const size = { width, height: width === 393 ? 852 : 900 };

		test(`${TAG} ${theme} ${width}px: J2 a reply on an image pin opens the image at its thread, then the next pin`, async ({ page, request }, testInfo) => {
			test.setTimeout(120_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize(size);
			await page.emulateMedia({ colorScheme: theme });
			seconds = 0;
			await freezeClock(page);
			const seeded = await seedImageDocument(page, request, 'launch-walkthrough', width === 393 ? null : 420);
			await onlyThread(request, seeded.sessionId);
			sqlite([['UPDATE threads SET title=? WHERE id=?', [imageThreadTitle, seeded.sessionId]]]);
			const image = '![Mock screen](shots/screen.png)';
			const from = DOC.indexOf(image);
			const ids: string[] = [];
			for (const [index, region] of [{ x: 0.1, y: 0.15, w: 0.25, h: 0.2 }, { x: 0.55, y: 0.5, w: 0.25, h: 0.2 }].entries()) {
				const root = await request.post(`/api/artifacts/${seeded.artifactId}/comments`, {
					params: { token: TEST_TOKEN },
					data: { body: index === 0 ? 'Is this the final header?' : 'Does the save button move here?', anchor_text: image, from_off: from, to_off: from + image.length, region, delivery_policy: 'immediate' },
				});
				expect(root.ok(), await root.text()).toBeTruthy();
				ids.push((await root.json()).comment.id as string);
			}
			tidy(seeded.artifactId);
			addAgentReply(seeded.artifactId, ids[0], seeded.sessionId, 'Yes, the header is final. I attached the export.');
			addAgentReply(seeded.artifactId, ids[1], seeded.sessionId, 'It stays where it is; only its label changed.');
			for (const message of IMAGE_CHAT) await injectMessage(request, seeded.sessionId, message, TEST_TOKEN);
			await loginByUrl(page);
			if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await sidebarThread(page, imageThreadTitle).click();
			await expect(page.locator('.cm-md-image img').first()).toBeVisible();
			// Read from below the image, where both pins are off frame: with a pin on screen there is nothing to be taken to and
			// the band would rightly show nothing.
			await page.locator('.cm-scroller').evaluate((el) => { el.scrollTop = el.scrollHeight; });
			await holdDwell(page, true);
			await expectControl(page, '2 new');
			await expectControlPoints(page, 2, 'above');
			await holdDwell(page, false);
			// Opening the document delivers the two pin comments, and the delivery toast sits at the bottom of the same cell.
			const delivered = page.locator('.toast').filter({ hasText: /comments? sent/ });
			await expect(delivered).toBeVisible();
			const toastBox = (await delivered.boundingBox())!;
			const noticeBox = (await controlBox(page).boundingBox())!;
			expect(toastBox.y + toastBox.height).toBeLessThanOrEqual(noticeBox.y);

			await expect(delivered).toHaveCount(0, { timeout: 15_000 });

			await holdDwell(page, true);
			if (width === 1440) await page.evaluate(() => {
				const samples: boolean[] = [];
				(window as unknown as { __documentJumpHit: boolean[] }).__documentJumpHit = samples;
				const sample = () => {
					if (document.querySelector('.image-overlay')) {
						const controls = [...document.querySelectorAll<HTMLElement>('[data-doc-jump]:not(.image-overlay [data-doc-jump])')];
						samples.push(controls.some(node => {
							const box = node.getBoundingClientRect();
							const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
							return !!hit && node.contains(hit);
						}));
					}
					if (samples.length < 120) requestAnimationFrame(sample);
				};
				requestAnimationFrame(sample);
			});
			await controlButton(page).click();
			// The open pin is excluded from the count, and while the image is open the document's band keeps out at either width.
			await expect(overlay(page)).toBeVisible();
			await expect(page.getByText('Yes, the header is final. I attached the export.')).toBeVisible();
			if (width === 393) {
				await expect(controlBox(page)).toHaveCount(0);
			} else {
				// The document's control is never drawn and never hittable while an image is open, at either width: `covered`
				// is what the surface calls an image open over the document, and the band keeps out of it. The sampled hits
				// still prove the point on the way - the control may be completing its outro as the image opens, and it must
				// not answer a pointer from under the overlay while it does. Measured: the document's band instance reads
				// covered true with no row in the DOM, and its own landing for the remaining thread is the text line above
				// the fold, which the reader will be offered again the moment the image closes.
				await expect(page.locator('[data-doc-jump][inert]')).toHaveCount(0);
				const documentJumpHits = await page.evaluate(() => (window as unknown as { __documentJumpHit: boolean[] }).__documentJumpHit);
				expect(documentJumpHits.length).toBeGreaterThan(0);
				expect(documentJumpHits.some(Boolean)).toBe(false);
				await expect(controlBox(page)).toHaveCount(0);
				// The viewer keeps its own control away here too, with both pins in frame (its spec asserts that at Fit).
				await expect(overlay(page).locator('[data-doc-jump]')).toHaveCount(0);
			}

			await holdDwell(page, false);
			// Back to the document: one waiting reply is left. Its pin sits on the image the reader was just looking at, so the
			// band shows nothing; it returns when the reader scrolls that image out of view, and the next tap goes to that pin.
			await backToDocument(page);
			await expect.poll(() => unreadCountInStore(page, seeded.artifactId)).toBe(1);
			await page.locator('.cm-scroller').evaluate((el) => { el.scrollTop = el.scrollHeight; });
			await holdDwell(page, true);
			await expectControl(page, '1 new');

			await holdDwell(page, true);
			await controlButton(page).click();
			await expect(overlay(page)).toBeVisible();
			await expect(page.getByText('It stays where it is; only its label changed.')).toBeVisible();

			await holdDwell(page, false);
			await backToDocument(page);
			// Everything read: the control is gone.
			await expect(controlBox(page)).toHaveCount(0);

			expect(DOC_IMAGE_NATURAL.width).toBeGreaterThan(0);
		});
	}
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		const size = { width, height: width === 393 ? 852 : 900 };

		test(`${TAG} ${theme} ${width}px: J3 arriving from the chat's control lands on the first reply; closing the list leaves the document's control for the rest`, async ({ page, request }, testInfo) => {
			test.setTimeout(90_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize(size);
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, threadTitle);
			await onlyThread(request, threadId);
			const doc = await seedTextDocument(page, request, threadId, `j3-${theme}-${width}`, 'launch-plan.md', [2, 6, 10]);
			await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'chat');
			await openThread(page, threadTitle);
			await page.evaluate(async () => (await import('/src/lib/stores/attentionDock.svelte.ts')).reload());
			// The chat's notice names the document; the document's own control, drawn once it opens, does not.
			const chatChip = page.locator('.reply-chip-notice').filter({ hasText: 'launch-plan.md' }).getByRole('button');
			await expect(chatChip).toContainText('3 new replies · launch-plan.md');
			await expect(chatComposer(page)).toBeVisible();

			await holdDwell(page, true);
			await chatChip.click();
			await expect(page.locator(`[data-comment-id="${doc.roots[2]}"]`)).toHaveClass(/focused/);
			await expect(page.locator('.doc .cm-content')).toContainText('Q4 launch plan');

			await holdDwell(page, false);
			// The first reply is read after its dwell. Put the comments list away: the document's control is back, counting the other two.
			await awaitFocusedRead(page);
			await closeComments(page);
			await expect(page.locator('[data-doc-comments-drawer]')).toHaveCount(0);
			await expectControl(page, '2 new');

		});
	}
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		const size = { width, height: width === 393 ? 852 : 900 };

		test(`${TAG} ${theme} ${width}px: States: one reply, twelve replies, a long document name, and all read`, async ({ page, request }, testInfo) => {
			test.setTimeout(120_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize(size);
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, threadTitle);
			await onlyThread(request, threadId);
			const doc = await seedTextDocument(page, request, threadId, `j4-${theme}-${width}`, LONG_NAME, [11]);
			await stageWorkspace(page, threadId, doc.docPath, LONG_NAME, 'artifact');
			await openThread(page, threadTitle);
			await expect(page.locator('.doc .cm-content')).toContainText('Q4 launch plan');
			await expectControl(page, '1 new');

			// Eleven more land while the reader is here: the same control, a bigger number.
			await page.waitForTimeout(1100);
			for (let index = 0; index < 11; index += 1) addAgentReply(doc.artifactId, doc.roots[index], threadId, SECTIONS[index].reply, 'Maya Chen — Customer Support Operations');
			await reloadComments(page, doc.artifactId);
			// The backend pushes the new queue to the chat beside the document; here it is read again.
			await page.evaluate(async () => (await import('/src/lib/stores/attentionDock.svelte.ts')).reload());
			await expectControl(page, '12 new');

			// Beside the comments pill: both stay whole and clear of each other.
			const pill = page.getByRole('button', { name: /^\d+ threads?\b/ });
			await expect(pill).toBeVisible();
			const [control, pillBox] = await Promise.all([controlBox(page).boundingBox(), pill.boundingBox()]);
			const overlaps = control!.x < pillBox!.x + pillBox!.width && pillBox!.x < control!.x + control!.width && control!.y < pillBox!.y + pillBox!.height && pillBox!.y < control!.y + control!.height;
			expect(overlaps, 'the control and the comments pill do not overlap').toBe(false);
			// Tapping through every waiting thread reads them all: the control leaves.
			for (let step = 0; step < 12; step += 1) {
				if (!(await controlBox(page).count())) break;
				await controlButton(page).click();
				// The comments list opens over the document and covers the control; stay until the arrival reads it, then put it away to tap again.
				await awaitFocusedRead(page);
				await closeComments(page);
				await expect(page.locator('[data-doc-comments-drawer]')).toHaveCount(0);
			}
			await expect(controlBox(page)).toHaveCount(0);

		});
	}
}

for (const [theme, width] of [['light', 1440], ['dark', 393]] as const) {
	// The design's three states, photographed where Steve reads them: the frame at 1440 in light, the phone at 393 in dark.
	test(`${TAG} ${theme} ${width}px: the control appears only for unread text that is off screen, and points at it`, async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		skipMismatchedProject(width, testInfo.project.name);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: theme });
		const still = (state: string) => stillPath(width, theme, state);
		const threadId = await createSession(request, threadTitle);
		await onlyThread(request, threadId);
		const doc = await seedTextDocument(page, request, threadId, `sight-${theme}-${width}`, 'launch-plan.md', [8, 10]);
		await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
		await openThread(page, threadTitle);
		await expect(page.locator('.doc .cm-content')).toContainText('Q4 launch plan');
		// Nothing is read while the reader moves, so every state below is the control's own, not a count falling.
		await holdDwell(page, true);

		// Opened at the top: both threads are below, so it points down at the nearer one.
		await expectControl(page, '2 new');
		await expectControlPoints(page, 2, 'below');
		await page.screenshot({ path: still('below-target') });

		// Past the end: both are behind now, so the same control points up at the nearer one.
		await page.locator('.cm-scroller').evaluate((el) => { el.scrollTop = el.scrollHeight; });
		await expectControl(page, '2 new');
		await expectControlPoints(page, 2, 'above');
		await page.screenshot({ path: still('above-target') });

		await page.locator('.cm-scroller').evaluate((el) => { el.scrollTop = 0; });
		await expectControl(page, '2 new');
		await expectControlPoints(page, 2, 'below');

		// One thread left, its text still below the fold: the control stays, still counting it.
		sqlite([['DELETE FROM doc_comments WHERE artifact_id=? AND parent_id=?', [doc.artifactId, doc.roots[10]]]]);
		await reloadComments(page, doc.artifactId);
		await expectControl(page, '1 new');
		await expectControlPoints(page, 1, 'below');
	});
}

for (const [theme, width] of [['light', 1440], ['dark', 393]] as const) {
	// The design's third state, and the one case with nothing to jump to: the unread text is already on screen.
	test(`${TAG} ${theme} ${width}px hidden: unread text the reader can already see draws no control`, async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		skipMismatchedProject(width, testInfo.project.name);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: theme });
		const threadId = await createSession(request, threadTitle);
		await onlyThread(request, threadId);
		const doc = await seedTextDocument(page, request, threadId, `hidden-${theme}-${width}`, 'launch-plan.md', [2]);
		await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
		await openThread(page, threadTitle);
		await expect(page.locator('.doc .cm-content')).toContainText('Q4 launch plan');
		await holdDwell(page, true);
		await expect.poll(() => unreadCountInStore(page, doc.artifactId)).toBe(1);
		// Put the reader on the thread's own line. A document paints only the lines around the reader — at 393 that line
		// is not in the page yet — so reach it the way a reader does: scroll toward it until it is drawn, then stand on it.
		const unreadLine = page.locator('.cm-line').filter({ hasText: SECTIONS[2].anchor }).first();
		await page.locator('.doc-main').hover();
		await expect.poll(async () => {
			if (await unreadLine.count() > 0) return true;
			await page.mouse.wheel(0, 500);
			return false;
		}, { message: `the document paints the unread line (${SECTIONS[2].anchor})`, timeout: 30_000 }).toBe(true);
		await unreadLine.evaluate((el) => el.scrollIntoView({ block: 'center' }));
		// The thread is unread and its own line is inside the pane: this is the control's own absence, not a count that fell,
		// and nothing can bring it further into view.
		await expect.poll(async () => (await anchorBox(page, SECTIONS[2].anchor))?.whole === true, { message: `the unread line (${SECTIONS[2].anchor}) sits inside the visible pane` }).toBe(true);
		await expect(controlButton(page)).toHaveCount(0);
		await page.screenshot({ path: stillPath(width, theme, 'hidden') });
		await holdDwell(page, false);
	});
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		test(`${TAG} ${theme} ${width}px: multiple replies in one thread and a phone draft`, async ({ page, request }, testInfo) => {
			test.setTimeout(90_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, threadTitle);
			await onlyThread(request, threadId);
			const doc = await seedTextDocument(page, request, threadId, `multi-${theme}-${width}`, 'launch-plan.md', [8]);
			addAgentReply(doc.artifactId, doc.roots[8], threadId, 'The handover is ready.');
			addAgentReply(doc.artifactId, doc.roots[8], threadId, 'I added the final owner.');
			await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
			await openThread(page, threadTitle);
			await expectControl(page, '3 new');

			if (width === 393) {
				const line = page.locator('.cm-line').filter({ hasText: 'This workstream covers the first-run screens' }).first();
				await line.dblclick({ position: { x: 20, y: 8 } });
				await page.locator('.sel-comment').click();
				await page.getByPlaceholder('Comment, or @ to tag an agent').fill('Please confirm the handover.');
				await expect(page.locator('[data-adaptive-drawer].drawer-backdrop')).toBeVisible();
				await expect(controlBox(page)).toHaveCount(0);

				await page.getByRole('button', { name: 'Cancel', exact: true }).click();
				await expectControl(page, '3 new');

			} else {
				const line = page.locator('.cm-line').filter({ hasText: 'This workstream covers the first-run screens' }).first();
				await line.dblclick({ position: { x: 20, y: 8 } });
				await page.locator('.sel-comment').click();
				const draft = page.getByPlaceholder('Comment, or @ to tag an agent');
				await draft.fill('Please confirm the handover.');
				await controlButton(page).click();
				const savedDraft = await page.evaluate(async () => {
					const { commentDraft } = await import('/src/lib/stores/commentDraft.ts');
					let body: string | undefined;
					const unsubscribe = commentDraft.subscribe((draft) => { body = draft?.body; });
					unsubscribe();
					return body;
				});
				expect(savedDraft).toBe('Please confirm the handover.');
			}
		});
	}
}

test.describe('jump control motion evidence', () => {
	for (const width of [1440, 393]) {
		for (const reduced of [false, true]) {
			test(`${TAG} motion ${width}px ${reduced ? 'reduced' : 'standard'}`, async ({ page, request }, testInfo) => {
				test.setTimeout(90_000);
				test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
				skipMismatchedProject(width, testInfo.project.name);
				await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
				await page.emulateMedia({ reducedMotion: reduced ? 'reduce' : 'no-preference' });
				const threadId = await createSession(request, threadTitle);
				await onlyThread(request, threadId);
				const doc = await seedTextDocument(page, request, threadId, `motion-${width}-${reduced}`, 'launch-plan.md', []);
				await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
				await openThread(page, threadTitle);
				await expect(controlBox(page)).toHaveCount(0);
				const durations = await page.evaluate(async () => {
					const { ms } = await import('/src/lib/popoverMotion.ts');
					return { standard: ms('--dur-standard'), tight: ms('--dur-tight') };
				});

				expect(durations.standard).toBeGreaterThan(0);
				expect(durations.tight).toBeGreaterThan(0);
				await page.waitForTimeout(600);
				addAgentReply(doc.artifactId, doc.roots[8], threadId, 'The owner is confirmed.');
				await startMotionProbe(page);
				await reloadComments(page, doc.artifactId);
				await expectControl(page, '1 new');
				const entryEvents = await saveMotionProbe(page);
				const entry = entryEvents.filter(event => String(event.target).includes('doc-jump')).flatMap(event => event.animations).find(animation => animation.timing.duration > 0);
				expect(entry?.timing.duration).toBe(reduced ? durations.tight : durations.standard);
				if (reduced) expect(new Set(entry!.keyframes.map(frame => frame.transform)).size).toBe(1);
				await startMotionProbe(page);
				await controlButton(page).click();
				await expect(page.locator(`[data-comment-id="${doc.roots[8]}"]`)).toHaveClass(/focused/);
				await saveMotionProbe(page);
				await page.waitForTimeout(2000);
				await startMotionProbe(page);
				await closeComments(page);
				await expect(controlBox(page)).toHaveCount(0);
				await saveMotionProbe(page);
				// Stimulate a second arrival and a viewed event while the document is visible, so the recording includes the control's own exit.
				addAgentReply(doc.artifactId, doc.roots[8], threadId, 'The handover is ready.');
				await reloadComments(page, doc.artifactId);
				await expectControl(page, '1 new');
				await page.waitForTimeout(700);
				await startMotionProbe(page);
				await page.evaluate(async ({ artifactId, rootId }) => {
					const { docComments, markThreadViewed } = await import('/src/lib/stores/docComments.ts');
					let replyId: string | undefined;
					const unsubscribe = docComments.subscribe((all) => { replyId = all[artifactId]?.find((thread) => thread.id === rootId)?.newReplyIds?.at(-1); });
					unsubscribe();
					if (!replyId) throw new Error('No unread reply to mark viewed');
					await markThreadViewed(artifactId, rootId, replyId);
				}, { artifactId: doc.artifactId, rootId: doc.roots[8] });
				await expect(controlBox(page)).toHaveCount(0);
				const exitEvents = await saveMotionProbe(page);
				const exit = exitEvents.filter(event => String(event.target).includes('doc-jump')).flatMap(event => event.animations).find(animation => animation.timing.duration > 0);
				expect(exit?.timing.duration).toBeCloseTo(durations.standard * 0.7);
				expect(exit?.keyframes.every(frame => frame.transform === undefined)).toBe(true);
			});
		}
	}
});

test.describe('the jump control and held Send controls', () => {
	for (const theme of ['light', 'dark'] as const) {
		test(`${TAG} held Send 393 ${theme}`, async ({ page, request }, info) => {
			test.setTimeout(90_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1' || !info.project.name.endsWith('mobile'));
			await page.setViewportSize({ width: 393, height: 852 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, threadTitle);
			await onlyThread(request, threadId);
			const doc = await seedTextDocument(page, request, threadId, `r5-send-${theme}`, 'launch-plan.md', [8, 10]);
			await holdDocumentComments(request);
			const from = TEXT.indexOf(SECTIONS[0].anchor);
			await seedHeldComments(request, doc.artifactId, 1, 'Review', { text: SECTIONS[0].anchor, from, to: from + SECTIONS[0].anchor.length });
			await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
			await openThread(page, threadTitle);
			await expectControl(page, '2 new');
			const send = headerSend(page);
			await expect(send).toBeVisible();
			await page.waitForTimeout(500); // Let the sender's entrance slot finish before measuring.
			const geometry = await send.evaluate(node => {
				const rect = node.getBoundingClientRect(), hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
				return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, hit: !!hit && (node === hit || node.contains(hit)) };
			});
			expect(geometry.x).toBeGreaterThanOrEqual(0);
			expect(geometry.x + geometry.width).toBeLessThanOrEqual(393);
			expect(geometry.y).toBeGreaterThanOrEqual(0);
			expect(geometry.y + geometry.height).toBeLessThanOrEqual(852);
			expect(geometry.hit).toBe(true);
			const cdp = await page.context().newCDPSession(page);
			await cdp.send('DOM.enable'); await cdp.send('CSS.enable');
			const { root } = await cdp.send('DOM.getDocument');
			const { nodeId } = await cdp.send('DOM.querySelector', { nodeId: root.nodeId, selector: '.document-header .sending-now button' });
			expect(nodeId).toBeGreaterThan(0);
			const beforeColor = await send.evaluate(node => getComputedStyle(node).backgroundColor);

			await cdp.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: ['active'] });
			expect(await send.evaluate(node => node.matches(':active'))).toBe(true);
			await page.waitForTimeout(200);
			const activeColor = await send.evaluate(node => getComputedStyle(node).backgroundColor);

			await cdp.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: [] }); await cdp.detach();
			expect(activeColor).not.toBe(beforeColor);

		});
	}

	for (const reduced of [false, true]) {
		test(`${TAG} modal outro 393 ${reduced ? 'reduced' : 'standard'}`, async ({ page, request }, info) => {
			test.setTimeout(90_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1' || !info.project.name.endsWith('mobile'));
			await page.setViewportSize({ width: 393, height: 852 });
			await page.emulateMedia({ reducedMotion: reduced ? 'reduce' : 'no-preference' });
			const threadId = await createSession(request, threadTitle);
			await onlyThread(request, threadId);
			const doc = await seedTextDocument(page, request, threadId, `r5-scrim-${reduced}`, 'launch-plan.md', [8, 10]);
			await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
			await openThread(page, threadTitle);
			await expectControl(page, '2 new');
			await page.evaluate(() => {
				const chip = document.querySelector<HTMLElement>('[data-doc-jump]')!;
				const button = chip.querySelector('button')!;
				button.addEventListener('click', () => {
					const rect = chip.getBoundingClientRect(), x = rect.x + rect.width / 2, y = rect.y + rect.height / 2;
					const frames: unknown[] = [];
					(window as unknown as { __r5Frames: unknown[] }).__r5Frames = frames;
					const sample = (time: number) => {
						const current = document.querySelector<HTMLElement>('[data-doc-jump]');
						const scrim = document.querySelector<HTMLElement>('.drawer-backdrop:not(.outro-shield)');
						const top = document.elementFromPoint(x, y);
						const stack = document.elementsFromPoint(x, y);
						const chipIndex = current ? stack.findIndex(node => node === current || current.contains(node)) : -1;
						const scrimIndex = scrim ? stack.findIndex(node => node === scrim || scrim.contains(node)) : -1;
						frames.push({ time, chipPresent: !!current, chipOpacity: current ? getComputedStyle(current).opacity : null, chipPointerEvents: current ? getComputedStyle(current).pointerEvents : null, scrimPresent: !!scrim, scrimOpacity: scrim ? getComputedStyle(scrim).opacity : null, scrimZ: scrim ? getComputedStyle(scrim).zIndex : null, top: top ? `${top.tagName}.${top.className}` : null, hitChip: !!current && !!top && (current === top || current.contains(top)), chipIndex, scrimIndex, chipAheadInHitStack: chipIndex >= 0 && (scrimIndex < 0 || chipIndex < scrimIndex) });
						if (current || !scrim) requestAnimationFrame(sample);
						else (window as unknown as { __r5Done: boolean }).__r5Done = true;
					};
					requestAnimationFrame(sample);
				}, { once: true, capture: true });
			});
			await controlButton(page).click();
			await page.waitForFunction(() => (window as unknown as { __r5Done?: boolean }).__r5Done === true, null, { timeout: 5000 });
			const frames = await page.evaluate(() => (window as unknown as { __r5Frames: Array<{ chipPresent: boolean; scrimPresent: boolean; hitChip: boolean; chipAheadInHitStack: boolean }> }).__r5Frames);

			expect(frames.some(frame => frame.chipPresent)).toBe(true);
			expect(frames.every(frame => frame.scrimPresent && !frame.hitChip && !frame.chipAheadInHitStack)).toBe(true);
		});
	}
});

test.describe('the jump control and the document toolbar', () => {
	for (const theme of ['light', 'dark'] as const) {
		test(`${TAG} no held comments 393 ${theme}`, async ({ page, request }, info) => {
			test.setTimeout(90_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1' || !info.project.name.endsWith('mobile'));
			await page.setViewportSize({ width: 393, height: 852 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, threadTitle);
			await onlyThread(request, threadId);
			const doc = await seedTextDocument(page, request, threadId, `r6-no-held-${theme}`, 'launch-plan.md', [8, 10]);
			await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
			await openThread(page, threadTitle);
			await expectControl(page, '2 new');
			await expect(page.locator('.document-header .sending-now')).toHaveCount(0);

		});
	}
	test(`${TAG} held Send desktop pane 420`, async ({ page, request }, info) => {
		test.setTimeout(90_000);
		test.skip(process.env.CORSIA_E2E_MOCK !== '1' || info.project.name.endsWith('mobile'));
		await page.setViewportSize({ width: 1440, height: 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		const threadId = await createSession(request, threadTitle);
		await onlyThread(request, threadId);
		const doc = await seedTextDocument(page, request, threadId, 'r6-held-420', 'launch-plan.md', [8, 10]);
		await holdDocumentComments(request);
		const from = TEXT.indexOf(SECTIONS[0].anchor);
		await seedHeldComments(request, doc.artifactId, 1, 'Review', { text: SECTIONS[0].anchor, from, to: from + SECTIONS[0].anchor.length });
		await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
		await openThread(page, threadTitle);
		await expectControl(page, '2 new');
		await page.waitForTimeout(600);
		const geometry = await headerSend(page).evaluate(node => {
			const rect = node.getBoundingClientRect();
			const canvas = node.closest('.document-canvas')!.getBoundingClientRect();
			const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
			return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, canvasX: canvas.x, canvasWidth: canvas.width, whole: rect.x >= canvas.x && rect.right <= canvas.right && rect.y >= canvas.y && rect.bottom <= canvas.bottom, hit: !!hit && (hit === node || node.contains(hit)) };
		});

		expect(geometry.canvasWidth).toBe(420);
		expect(geometry.whole && geometry.hit).toBe(true);
	});
});
