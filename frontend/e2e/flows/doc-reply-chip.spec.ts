import { execFileSync } from 'node:child_process';
import { createHash } from 'node:crypto';
import { mkdirSync, readFileSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { chatComposer, createSession, loginByUrl, sendButton, sidebarThread, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { injectMessage, seedMockResponses } from '../support/mock';
import { DOC, DOC_IMAGE_NATURAL, overlay, putSheetAway, seedImageDocument } from '../support/docImageComments';
import { headerSend, holdDocumentComments, seedHeldComments } from '../support/heldComments';
import { hitBox } from '../support/hitBox';

// Evidence frames for the document reply chip (R5): the chat's own "N new replies" chip, floated over the document it counts.
// Mock-mode only: every state is seeded straight into the disposable database, and the clock is frozen so a frame is repeatable.

test.use({ video: process.env.REPLY_CHIP_VIDEO === '1' ? 'on' : 'retain-on-failure' });

const TAG = '@surface:doc-reply-chip';
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const screenshotDir = process.env.REPLY_CHIP_SCREENSHOT_DIR ?? process.env.CORSIA_E2E_WORKSPACE;
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

async function settled(page: Page): Promise<Buffer> {
	await page.waitForFunction(() => { const shell = document.querySelector('.app'); return !shell || getComputedStyle(shell).opacity === '1'; });
	await expect(page.getByText(/^Loading/)).toHaveCount(0);
	await page.evaluate(() => document.fonts.ready.then(() => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)))));
	let previous: Buffer | null = null;
	for (let attempt = 0; attempt < 12; attempt += 1) {
		const frame = await page.screenshot({ fullPage: false, scale: 'css', animations: 'disabled' });
		if (previous?.equals(frame)) return frame;
		previous = frame;
		await page.waitForTimeout(300);
	}
	throw new Error('The page never settled, so no frame was taken');
}

async function shot(page: Page, name: string): Promise<void> {
	if (!screenshotDir) return;
	// A fade-in (the chip's arrival) is over before the frame: wait it out, then require two identical captures.
	await page.evaluate(() => Promise.allSettled(document.getAnimations().filter((a) => a.effect?.getTiming().iterations !== Infinity).map((a) => a.finished)));
	await page.evaluate(async () => {
		const { coreSettings } = await import('/src/lib/stores/settings.ts');
		coreSettings.update((current) => ({ ...current, features: { ...current?.features, debug_version_overlay: false } }));
	});
	await expect(page.locator('.version-overlay')).toHaveCount(0);
	// Park the pointer on inert space so no control keeps the hover it got from the last click.
	const view = page.viewportSize()!;
	await page.mouse.move(view.width - 2, Math.round(view.height / 2));
	const frame = await settled(page);
	expect(await page.evaluate(() => document.body.innerText), `Frame ${name} shows test wording`).not.toMatch(/\be2e\b|fixture|Another agent|e2e-/i);
	mkdirSync(screenshotDir, { recursive: true });
	writeFileSync(path.join(screenshotDir, `${name}.png`), frame);
}

async function startMotionProbe(page: Page): Promise<void> {
	await page.evaluate(() => {
		type Probe = { events: unknown[]; cleanup: () => void };
		const w = window as unknown as { __replyMotion?: Probe };
		w.__replyMotion?.cleanup();
		const events: unknown[] = [];
		const selector = '[data-doc-reply-chip], .drawer-motion, .drawer-backdrop:not(.outro-shield), [data-comment-id]';
		const relevant = (node: Element) => node.matches(selector) || !!node.closest('[data-doc-reply-chip], .drawer-motion');
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

async function saveMotionProbe(page: Page, name: string): Promise<Array<{ target: string; animations: Array<{ timing: { duration: number }; keyframes: Array<{ transform?: string; opacity?: string }> }> }>> {
	await page.waitForTimeout(600);
	const events = await page.evaluate(() => {
		const probe = (window as unknown as { __replyMotion: { events: unknown[]; cleanup: () => void } }).__replyMotion;
		probe.cleanup();
		return probe.events;
	});
	if (screenshotDir) {
		mkdirSync(screenshotDir, { recursive: true });
		writeFileSync(path.join(screenshotDir, `${name}-events.json`), JSON.stringify(events));
	}
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
		threads: { [threadId]: { tabs: [{ instanceId: 'reply-chip', artifactId: null, path: docPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'reply-chip', visible: true, activeSurface: surface } },
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

const chipButton = (page: Page) => page.locator('[data-doc-reply-chip]').getByRole('button');
const chipBox = (page: Page) => page.locator('[data-doc-reply-chip]');

/** The button's extended hit area is a pseudo-element, not the visible face's bounding box. */
async function chipHitBox(page: Page) {
	return chipButton(page).evaluate((button) => {
		const face = button.getBoundingClientRect();
		const height = Number.parseFloat(getComputedStyle(button, '::after').height);
		return { x: face.x, y: face.y + (face.height - height) / 2, width: face.width, height };
	});
}

/** The chip, once drawn: its text, and that nothing else on the document's surface sits under it. */
async function expectChip(page: Page, text: string): Promise<void> {
	await expect(chipButton(page)).toHaveText(new RegExp(text));
	const view = page.viewportSize()!;
	// Opening the artifact on mobile can still be moving its whole surface when its text appears.
	await expect.poll(async () => {
		const chip = await chipBox(page).boundingBox();
		return !!chip && chip.x >= 0 && chip.x + chip.width <= view.width + 0.5 && chip.y + chip.height <= view.height + 0.5;
	}, { message: 'the settled chip is inside the viewport' }).toBe(true);
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		const size = { width, height: width === 393 ? 852 : 900 };

		test(`${TAG} ${theme} ${width}px: J1 a reply below the fold is announced and the chip goes to it`, async ({ page, request }, testInfo) => {
			test.setTimeout(90_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize(size);
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			const doc = await seedTextDocument(page, request, threadId, `j1-${theme}-${width}`, 'launch-plan.md', [8, 10]);
			const heldFrom = TEXT.indexOf(SECTIONS[0].anchor);
			await holdDocumentComments(request);
			await seedHeldComments(request, doc.artifactId, 1, 'Review', { text: SECTIONS[0].anchor, from: heldFrom, to: heldFrom + SECTIONS[0].anchor.length });
			tidy(doc.artifactId);
			await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
			await openThread(page, THREAD_TITLE);
			await expect(page.locator('.doc .cm-content')).toContainText('Q4 launch plan');
			// Opened at the top: both replies are below the fold, the document says so once, in the chat's own chip.
			await expectChip(page, '2 new replies');
			await expect(headerSend(page)).toBeVisible();
			const sendBox = await hitBox(headerSend(page));
			const replyBox = await chipHitBox(page);
			expect(replyBox.height, 'reply target is at least 44px high').toBeGreaterThanOrEqual(44);
			expect(replyBox.width, 'reply target is at least 44px wide').toBeGreaterThanOrEqual(44);
			expect(replyBox.x < sendBox.x + sendBox.width && sendBox.x < replyBox.x + replyBox.width && replyBox.y < sendBox.y + sendBox.height && sendBox.y < replyBox.y + replyBox.height, 'held-comment Send stays clear of the reply target').toBe(false);
			await shot(page, `01-text-document-chip-${theme}-${width}`);
			// Capture the held Send after the caret evidence, so a missing press treatment cannot suppress it.
			await expect(chipBox(page).locator('.ct-new')).toBeVisible();
			// Probe the actual hit target at both vertical edges, outside the visual chip face.
			const edgeHits = await chipButton(page).evaluate((button) => {
				const box = button.getBoundingClientRect();
				const x = box.left + box.width / 2;
				const center = box.top + box.height / 2;
				return [-21, 21].map((offset) => button.contains(document.elementFromPoint(x, center + offset)));
			});
			expect(edgeHits).toEqual([true, true]);
			const anchor = page.locator('.cm-comment-anchor').first();
			await anchor.scrollIntoViewIfNeeded();
			const anchorBox = (await anchor.boundingBox())!;
			const editorBox = (await page.locator('.doc-main').boundingBox())!;
			const chipBounds = (await chipBox(page).boundingBox())!;
			expect(anchorBox.y).toBeGreaterThanOrEqual(editorBox.y - 0.5);
			expect(anchorBox.y + anchorBox.height).toBeLessThanOrEqual(editorBox.y + editorBox.height + 0.5);
			expect(anchorBox.y + anchorBox.height).toBeLessThanOrEqual(chipBounds.y);
			await shot(page, `01b-visible-anchor-with-chip-${theme}-${width}`);
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
			const chipAfter = (await chipBox(page).boundingBox())!;
			expect(caretBox.y + caretBox.height, 'caret stays inside the editor').toBeLessThanOrEqual(editorAfter.y + editorAfter.height + 0.5);
			expect(caretBox.y + caretBox.height, 'caret stays above the reply chip').toBeLessThanOrEqual(chipAfter.y);
			if (screenshotDir) {
				await expect(page.locator('.doc .cm-content')).toBeFocused();
				// This editor uses the native caret (no CodeMirror drawSelection layer). Preserve two blink phases for pixel inspection.
				const first = await page.screenshot({ scale: 'css', animations: 'disabled', caret: 'initial' });
				await page.waitForTimeout(600);
				const second = await page.screenshot({ scale: 'css', animations: 'disabled', caret: 'initial' });
				const neighbour = readFileSync(path.join(screenshotDir, `01b-visible-anchor-with-chip-${theme}-${width}.png`));
				writeFileSync(path.join(screenshotDir, `01c-caret-diagnostic-${theme}-${width}.json`), JSON.stringify({ caretBox, framesDiffer: !first.equals(second) }));
				if (!first.equals(second)) {
					expect(first.equals(neighbour) && second.equals(neighbour), 'at least one caret candidate differs from the anchor frame').toBe(false);
					writeFileSync(path.join(screenshotDir, `01c-caret-a-${theme}-${width}.png`), first);
					writeFileSync(path.join(screenshotDir, `01c-caret-b-${theme}-${width}.png`), second);
				}
				await page.locator('.cm-scroller').evaluate((el) => { el.scrollTop = 0; });
				if (width === 1440) {
					await page.getByRole('button', { name: 'Toggle panel' }).click();
					await page.evaluate(async () => { const { splitWidth } = await import('/src/lib/stores/artifactWorkspace.ts'); splitWidth.set(650); });
					await expect.poll(() => page.locator('.document-canvas').evaluate(node => node.getBoundingClientRect().width)).toBeGreaterThanOrEqual(649);
				}
				const send = headerSend(page);
				await send.scrollIntoViewIfNeeded();
				if (width === 1440) {
					const sendRect = (await send.boundingBox())!, canvasRect = (await page.locator('.document-canvas').boundingBox())!;
					expect(sendRect.x).toBeGreaterThanOrEqual(canvasRect.x);
					expect(sendRect.x + sendRect.width).toBeLessThanOrEqual(canvasRect.x + canvasRect.width);
					writeFileSync(path.join(screenshotDir, `01d-wide-canvas-${theme}.json`), JSON.stringify({ canvasWidth: canvasRect.width, sendRect }));
				}
				const box = (await send.boundingBox())!;
				await page.mouse.move(box.x + box.width / 2, box.y + box.height / 2);
				const before = await page.screenshot({ scale: 'css', animations: 'disabled' });
				const beforeStyle = await send.evaluate((node) => { const style = getComputedStyle(node); return Object.fromEntries(Array.from(style).map((name) => [name, style.getPropertyValue(name)])); });
				const touch = width === 393 ? await page.context().newCDPSession(page) : null;
				if (touch) await touch.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: box.x + box.width / 2, y: box.y + box.height / 2 }] });
				else await page.mouse.down();
				const active = await send.evaluate((node) => node.matches(':active'));
				const pressedStyle = await send.evaluate((node) => { const style = getComputedStyle(node); return Object.fromEntries(Array.from(style).map((name) => [name, style.getPropertyValue(name)])); });
				const pressed = await page.screenshot({ scale: 'css', animations: 'disabled' });
				const changed = Object.keys(beforeStyle).filter((name) => beforeStyle[name] !== pressedStyle[name]).map((name) => ({ name, before: beforeStyle[name], pressed: pressedStyle[name] }));
				writeFileSync(path.join(screenshotDir, `01d-press-style-${theme}-${width}.json`), JSON.stringify({ active, changed, pixelsDiffer: !before.equals(pressed) }));
				if (touch) {
					await touch.send('Input.dispatchTouchEvent', { type: 'touchCancel', touchPoints: [] });
					await touch.detach();
				} else {
					await page.mouse.move(2, 2);
					await page.mouse.up();
				}
				if (active && changed.length) {
					expect(pressed.equals(before), 'pressed frame differs from unpressed neighbour').toBe(false);
					writeFileSync(path.join(screenshotDir, `01d-held-send-with-chip-${theme}-${width}.png`), pressed);
				}
				if (width === 393) {
					const cdp = await page.context().newCDPSession(page);
					await cdp.send('DOM.enable');
					await cdp.send('CSS.enable');
					const { root } = await cdp.send('DOM.getDocument');
					const { nodeId } = await cdp.send('DOM.querySelector', { nodeId: root.nodeId, selector: '.document-header .sending-now button' });
					expect(nodeId).toBeGreaterThan(0);
					await cdp.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: ['active'] });
					expect(await send.evaluate(node => node.matches(':active'))).toBe(true);
					await page.screenshot({ path: path.join(screenshotDir, `01d-forced-active-${theme}-393.png`), scale: 'css', animations: 'disabled' });
					await cdp.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: [] });
					await cdp.detach();
				}
			}
			await page.locator('.cm-scroller').evaluate((el) => { el.scrollTop = 0; });
			const before = await page.locator('.cm-scroller').evaluate((el) => el.scrollTop);
			await holdDwell(page, true);
			await chipButton(page).click();
			// The first waiting reply is brought into view, focused, and the comments list opens at it.
			await expect(page.locator(`[data-comment-id="${doc.roots[8]}"]`)).toHaveClass(/focused/);
			await expect.poll(() => page.locator('.cm-scroller').evaluate((el) => el.scrollTop)).toBeGreaterThan(before);
			await shot(page, `02-text-document-landed-${theme}-${width}`);
			if (screenshotDir) {
				const input = page.locator(`[data-comment-id="${doc.roots[8]}"] textarea[placeholder="Reply…"]`);
				await input.fill('Thanks, I will add the handover notes.');
				await input.focus();
				await expect(input).toBeFocused();
				await expect(chipBox(page)).toHaveCount(0);
				const first = await page.screenshot({ scale: 'css', animations: 'disabled', caret: 'initial' });
				await page.waitForTimeout(600);
				const second = await page.screenshot({ scale: 'css', animations: 'disabled', caret: 'initial' });
				writeFileSync(path.join(screenshotDir, `reply-caret-blink-${theme}-${width}.json`), JSON.stringify({ framesDiffer: !first.equals(second) }));
				if (!first.equals(second)) {
					writeFileSync(path.join(screenshotDir, `reply-caret-a-${theme}-${width}.png`), first);
					writeFileSync(path.join(screenshotDir, `reply-caret-b-${theme}-${width}.png`), second);
				}
			}
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
		const threadId = await createSession(request, THREAD_TITLE);
		await onlyThread(request, threadId);
		const doc = await seedTextDocument(page, request, threadId, `extremes-${width}`, 'launch-plan.md', []);
		const token = 'launchreadiness'.repeat(20);
		addAgentReply(doc.artifactId, doc.roots[8], threadId, token);
		await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
		await openThread(page, THREAD_TITLE);
		await expectChip(page, '1 new reply');
		await holdDwell(page, true);
		await chipButton(page).click();
		await expect(page.locator(`[data-comment-id="${doc.roots[8]}"]`)).toHaveClass(/focused/);
		await expect(page.getByText(token)).toBeVisible();
		await shot(page, `long-unbroken-reply-light-${width}`);
		await page.getByRole('button', { name: 'Close comments' }).click();
		const statement = "INSERT INTO doc_comments (id, artifact_id, parent_id, author, author_kind, author_thread_id, body, created_at, updated_at) VALUES (lower(hex(randomblob(16))), ?, ?, ?, 'agent', ?, ?, ?, ?)";
		sqlite(Array.from({ length: 119 }, (_, index) => [statement, [doc.artifactId, doc.roots[10], REPLIER, threadId, `Update ${index + 1}.`, null, null]]));
		await reloadComments(page, doc.artifactId);
		await expectChip(page, '120 new replies');
		await shot(page, `extreme-count-light-${width}`);
	});
}

for (const width of [1440, 393]) {
	test(`${TAG} viewed failure and partial response ${width}px`, async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		skipMismatchedProject(width, testInfo.project.name);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		const threadId = await createSession(request, THREAD_TITLE);
		await onlyThread(request, threadId);
		const doc = await seedTextDocument(page, request, threadId, `viewed-partial-${width}`, 'launch-plan.md', [8, 10]);
		await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
		await openThread(page, THREAD_TITLE);
		await expectChip(page, '2 new replies');
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
		await expectChip(page, '2 new replies');
		await shot(page, `viewed-500-retained-light-${width}`);
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
		await expectChip(page, '1 new reply');
		await shot(page, `partial-response-light-${width}`);
		await page.unroute(commentsRoute);
	});

	test(`${TAG} fetch failure ${width}px: retain the last reply count`, async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		skipMismatchedProject(width, testInfo.project.name);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		const threadId = await createSession(request, THREAD_TITLE);
		await onlyThread(request, threadId);
		const doc = await seedTextDocument(page, request, threadId, `fetch-failure-${width}`, 'launch-plan.md', [8, 10]);
		await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
		await openThread(page, THREAD_TITLE);
		await expectChip(page, '2 new replies');
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
			await expectChip(page, '2 new replies');
			await shot(page, `fetch-${failure}-light-${width}`);
			await page.unroute(route);
			await reloadComments(page, doc.artifactId);
			await expectChip(page, '2 new replies');
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
		const threadId = await createSession(request, THREAD_TITLE);
		await onlyThread(request, threadId);
		const doc = await seedTextDocument(page, request, threadId, `activity-${width}`, 'launch-plan.md', [8, 10]);
		const reviewerId = await createSession(request, 'Review colleague');
		await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
		await openThread(page, THREAD_TITLE);
		const baseline = process.env.REPLY_ACTIVITY_BASELINE === '1';
		if (baseline) await expect(chipBox(page)).toHaveCount(0);
		else await expectChip(page, '2 new replies');
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
		if (!baseline) {
			const activityBox = await hitBox(activity.getByRole('button'));
			const replyBox = await chipHitBox(page);
			expect(replyBox.height, 'reply target is at least 44px high').toBeGreaterThanOrEqual(44);
			expect(replyBox.x < activityBox.x + activityBox.width && activityBox.x < replyBox.x + replyBox.width && replyBox.y < activityBox.y + activityBox.height && activityBox.y < replyBox.y + replyBox.height, 'activity stays clear of the reply target').toBe(false);
		}
		if (!baseline && width === 393) {
			await expect.poll(async () => {
				const editor = await page.locator('.doc .ed-wrap').boundingBox();
				const pill = await activity.boundingBox();
				return !!editor && !!pill && editor.y + editor.height <= pill.y + 0.5;
			}, { message: 'mobile editor text clears the below activity notice' }).toBe(true);
		}
		await shot(page, `${baseline ? 'baseline-activity' : '01e-activity-with-chip'}-light-${width}`);
		if (!baseline) {
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
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			const doc = await seedTextDocument(page, request, threadId, `keyboard-${width}-${key}`, 'launch-plan.md', [8, 10]);
			await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
			await openThread(page, THREAD_TITLE);
			await expectChip(page, '2 new replies');
			await page.locator('body').focus();
			let reached = false;
			for (let i = 0; i < 100; i += 1) {
				await page.keyboard.press('Tab');
				if (await chipButton(page).evaluate((button) => button === document.activeElement)) { reached = true; break; }
			}
			expect(reached, 'notice is reachable in the tab order').toBe(true);
			await page.keyboard.press('Shift+Tab');
			await expect(chipButton(page)).not.toBeFocused();
			await page.keyboard.press('Tab');
			await expect(chipButton(page)).toBeFocused();
			await shot(page, `keyboard-chip-focus-${key}-light-${width}`);
			await holdDwell(page, true);
			await page.keyboard.press(key);
			await expect(page.locator(`[data-comment-id="${doc.roots[8]}"]`)).toHaveClass(/focused/);
			await expect(page.locator('[data-doc-comments-drawer]')).toBeVisible();
			await expect.poll(() => page.locator('[data-doc-comments-drawer]').evaluate((drawer) => drawer.contains(document.activeElement) || !!drawer.closest('[role="dialog"]')?.contains(document.activeElement))).toBe(true);
			await shot(page, `keyboard-destination-${key}-light-${width}`);
			await page.getByRole('button', { name: 'Close comments' }).click();
			await expect(page.locator('[data-doc-comments-drawer]')).toHaveCount(0);
			await expect(chipButton(page)).toBeFocused();
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
				const threadId = await createSession(request, THREAD_TITLE);
				await onlyThread(request, threadId);
				const doc = await seedTextDocument(page, request, threadId, `focus-${width}-${route}-${reduced}`, 'launch-plan.md', [8, 10]);
				await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
				await openThread(page, THREAD_TITLE);
				await expectChip(page, '2 new replies');
				await holdDwell(page, true);
				await chipButton(page).focus();
				await page.keyboard.press('Enter');
				const backdrop = page.locator('[data-adaptive-drawer].drawer-backdrop');
				await expect(backdrop).toBeVisible();
				await startMotionProbe(page);
				if (route === 'Escape') await page.keyboard.press('Escape');
				else {
					const box = (await backdrop.boundingBox())!;
					await page.mouse.click(box.x + 5, box.y + 5);
				}
				await expect(backdrop).toHaveCount(0);
				await expect(chipButton(page)).toBeFocused();
				await saveMotionProbe(page, `close-${route}-${width}-${reduced ? 'reduced' : 'standard'}`);
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
		const threadId = await createSession(request, THREAD_TITLE);
		await onlyThread(request, threadId);
		const doc = await seedTextDocument(page, request, threadId, `modal-${width}`, 'launch-plan.md', [8, 10]);
		await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
		await openThread(page, THREAD_TITLE);
		await expectChip(page, '2 new replies');
		const box = (await chipButton(page).boundingBox())!;
		await holdDwell(page, true);
		await chipButton(page).click();
		const sheet = page.locator('[data-adaptive-drawer].drawer-backdrop');
		await expect(sheet).toBeVisible();
		await expect(chipBox(page)).toHaveCount(0);
		const x = box.x + box.width / 2, y = box.y + box.height / 2;
		expect(await page.evaluate(({ x, y }) => !!document.elementFromPoint(x, y)?.closest('[data-adaptive-drawer]'), { x, y }), 'the sheet intercepts the former chip location').toBe(true);
		await page.getByRole('button', { name: 'Close comments' }).click();
		await expect(sheet.first()).toBeVisible();
		await expect(chipBox(page)).toHaveCount(0);
		expect(await page.evaluate(({ x, y }) => !!document.elementFromPoint(x, y)?.closest('[data-adaptive-drawer]'), { x, y }), 'the sheet intercepts the former chip location').toBe(true);
		await expect(sheet).toHaveCount(0);
		await expectChip(page, '2 new replies');
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
			sqlite([['UPDATE threads SET title=? WHERE id=?', [IMAGE_THREAD_TITLE, seeded.sessionId]]]);
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
			await page.getByText(IMAGE_THREAD_TITLE).first().click();
			await expect(page.locator('.cm-md-image img').first()).toBeVisible();
			await expectChip(page, '2 new replies');
			// Opening the document delivers the two pin comments, and the delivery toast sits at the bottom of the same cell.
			const delivered = page.locator('.toast').filter({ hasText: /comments? sent/ });
			await expect(delivered).toBeVisible();
			const toastBox = (await delivered.boundingBox())!;
			const noticeBox = (await chipBox(page).boundingBox())!;
			expect(toastBox.y + toastBox.height).toBeLessThanOrEqual(noticeBox.y);
			await shot(page, `03a-image-document-chip-with-delivery-toast-${theme}-${width}`);
			await expect(delivered).toHaveCount(0, { timeout: 15_000 });
			await shot(page, `03-image-document-chip-${theme}-${width}`);
			await holdDwell(page, true);
			if (width === 1440) await page.evaluate(() => {
				const samples: boolean[] = [];
				(window as unknown as { __documentChipHit: boolean[] }).__documentChipHit = samples;
				const sample = () => {
					if (document.querySelector('.image-overlay')) {
						const chips = [...document.querySelectorAll<HTMLElement>('[data-doc-reply-chip]:not(.image-overlay [data-doc-reply-chip])')];
						samples.push(chips.some(node => {
							const box = node.getBoundingClientRect();
							const hit = document.elementFromPoint(box.x + box.width / 2, box.y + box.height / 2);
							return !!hit && node.contains(hit);
						}));
					}
					if (samples.length < 120) requestAnimationFrame(sample);
				};
				requestAnimationFrame(sample);
			});
			await chipButton(page).click();
			// The open pin is excluded from the count: desktop shows the other reply clear of the popover; the phone's modal hides the chip.
			await expect(overlay(page)).toBeVisible();
			await expect(page.getByText('Yes, the header is final. I attached the export.')).toBeVisible();
			if (width === 393) {
				await expect(chipBox(page)).toHaveCount(0);
			} else {
				// The document's covered chip may still be completing its outro as the image opens.
				await expect(page.locator('[data-doc-reply-chip][inert]')).toHaveCount(0);
				const documentChipHits = await page.evaluate(() => (window as unknown as { __documentChipHit: boolean[] }).__documentChipHit);
				expect(documentChipHits.length).toBeGreaterThan(0);
				expect(documentChipHits.some(Boolean)).toBe(false);
				await expectChip(page, '1 new reply');
				await expect(chipButton(page)).toHaveAttribute('title', '1 new reply');
				const popover = page.locator('[data-popover]').filter({ hasText: 'Yes, the header is final. I attached the export.' });
				await expect(popover).toBeVisible();
				const chip = await chipHitBox(page);
				expect(chip.height, 'reply target is at least 44px high').toBeGreaterThanOrEqual(44);
				const targets = [popover, ...await popover.locator('button, input, textarea, select, a[href], [role="button"]').all()];
				for (const target of targets) {
					if (!await target.isVisible()) continue;
					const box = target === popover ? (await target.boundingBox())! : await hitBox(target);
					expect(chip.x < box.x + box.width && box.x < chip.x + chip.width && chip.y < box.y + box.height && box.y < chip.y + chip.height, 'reply chip clears the pin popover and its controls').toBe(false);
				}
			}
			await shot(page, `04-image-pin-thread-open-${theme}-${width}`);
			await holdDwell(page, false);
			// Back to the document: one waiting reply is left and the chip says so; the next tap goes to the second pin.
			await backToDocument(page);
			await expectChip(page, '1 new reply');
			await shot(page, `05-image-document-one-left-${theme}-${width}`);
			await holdDwell(page, true);
			await chipButton(page).click();
			await expect(overlay(page)).toBeVisible();
			await expect(page.getByText('It stays where it is; only its label changed.')).toBeVisible();
			await shot(page, `06-image-second-pin-${theme}-${width}`);
			await holdDwell(page, false);
			await backToDocument(page);
			// Everything read: the chip is gone.
			await expect(chipBox(page)).toHaveCount(0);
			await shot(page, `07-image-document-all-read-${theme}-${width}`);
			expect(DOC_IMAGE_NATURAL.width).toBeGreaterThan(0);
		});
	}
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		const size = { width, height: width === 393 ? 852 : 900 };

		test(`${TAG} ${theme} ${width}px: J3 arriving from the chat chip lands on the first reply; closing the list leaves the document chip for the rest`, async ({ page, request }, testInfo) => {
			test.setTimeout(90_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize(size);
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			const doc = await seedTextDocument(page, request, threadId, `j3-${theme}-${width}`, 'launch-plan.md', [2, 6, 10]);
			await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'chat');
			await openThread(page, THREAD_TITLE);
			await page.evaluate(async () => (await import('/src/lib/stores/attentionDock.svelte.ts')).reload());
			// The chat's chip names the document; the document's own chip, drawn once it opens, does not.
			const chatChip = page.locator('.reply-chip-notice').filter({ hasText: 'launch-plan.md' }).getByRole('button');
			await expect(chatChip).toContainText('3 new replies · launch-plan.md');
			await expect(chatComposer(page)).toBeVisible();
			await shot(page, `08-chat-chip-reference-${theme}-${width}`);
			await holdDwell(page, true);
			await chatChip.click();
			await expect(page.locator(`[data-comment-id="${doc.roots[2]}"]`)).toHaveClass(/focused/);
			await expect(page.locator('.doc .cm-content')).toContainText('Q4 launch plan');
			await shot(page, `09-arrived-from-chat-${theme}-${width}`);
			await holdDwell(page, false);
			// The first reply is read after its dwell. Put the comments list away: the document's chip is back, counting the other two.
			await page.waitForTimeout(1700);
			await page.getByRole('button', { name: 'Close comments' }).click();
			await expect(page.locator('[data-doc-comments-drawer]')).toHaveCount(0);
			await expectChip(page, '2 new replies');
			await shot(page, `09b-after-arrival-comments-closed-${theme}-${width}`);
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
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			const doc = await seedTextDocument(page, request, threadId, `j4-${theme}-${width}`, LONG_NAME, [11]);
			await stageWorkspace(page, threadId, doc.docPath, LONG_NAME, 'artifact');
			await openThread(page, THREAD_TITLE);
			await expect(page.locator('.doc .cm-content')).toContainText('Q4 launch plan');
			await expectChip(page, '1 new reply');
			await shot(page, `10-one-reply-${theme}-${width}`);
			// Eleven more land while the reader is here: the same chip, a bigger number.
			await page.waitForTimeout(1100);
			for (let index = 0; index < 11; index += 1) addAgentReply(doc.artifactId, doc.roots[index], threadId, SECTIONS[index].reply, 'Maya Chen — Customer Support Operations');
			await reloadComments(page, doc.artifactId);
			// The backend pushes the new queue to the chat beside the document; here it is read again.
			await page.evaluate(async () => (await import('/src/lib/stores/attentionDock.svelte.ts')).reload());
			await expectChip(page, '12 new replies');
			await shot(page, `11-twelve-replies-long-names-${theme}-${width}`);
			// Beside the comments pill: both stay whole and clear of each other.
			const pill = page.getByRole('button', { name: /open threads?/ });
			await expect(pill).toBeVisible();
			const [chip, pillBox] = await Promise.all([chipBox(page).boundingBox(), pill.boundingBox()]);
			const overlaps = chip!.x < pillBox!.x + pillBox!.width && pillBox!.x < chip!.x + chip!.width && chip!.y < pillBox!.y + pillBox!.height && pillBox!.y < chip!.y + chip!.height;
			expect(overlaps, 'the chip and the comments pill do not overlap').toBe(false);
			// Tapping through every waiting thread reads them all: the chip leaves.
			for (let step = 0; step < 12; step += 1) {
				if (!(await chipBox(page).count())) break;
				await chipButton(page).click();
				// The comments list opens over the document and covers the chip; read it for the dwell, then put it away to tap again.
				await page.waitForTimeout(1700);
				await page.getByRole('button', { name: 'Close comments' }).click();
				await expect(page.locator('[data-doc-comments-drawer]')).toHaveCount(0);
			}
			await expect(chipBox(page)).toHaveCount(0);
			await shot(page, `12-all-read-${theme}-${width}`);
		});
	}
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		test(`${TAG} ${theme} ${width}px: multiple replies in one thread and a phone draft`, async ({ page, request }, testInfo) => {
			test.setTimeout(90_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			const doc = await seedTextDocument(page, request, threadId, `multi-${theme}-${width}`, 'launch-plan.md', [8]);
			addAgentReply(doc.artifactId, doc.roots[8], threadId, 'The handover is ready.');
			addAgentReply(doc.artifactId, doc.roots[8], threadId, 'I added the final owner.');
			await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
			await openThread(page, THREAD_TITLE);
			await expectChip(page, '3 new replies');
			await shot(page, `multi-replies-one-thread-${theme}-${width}`);
			if (width === 393) {
				const line = page.locator('.cm-line').filter({ hasText: 'This workstream covers the first-run screens' }).first();
				await line.dblclick({ position: { x: 20, y: 8 } });
				await page.locator('.sel-comment').click();
				await page.getByPlaceholder('Comment…').fill('Please confirm the handover.');
				await expect(page.locator('[data-adaptive-drawer].drawer-backdrop')).toBeVisible();
				await expect(chipBox(page)).toHaveCount(0);
				await shot(page, `phone-draft-with-replies-${theme}-${width}`);
				await page.getByRole('button', { name: 'Cancel', exact: true }).click();
				await expectChip(page, '3 new replies');
				await shot(page, `phone-after-draft-${theme}-${width}`);
			}
		});
	}
}

test.describe('reply chip motion evidence', () => {
	for (const width of [1440, 393]) {
		for (const reduced of [false, true]) {
			test(`${TAG} motion ${width}px ${reduced ? 'reduced' : 'standard'}`, async ({ page, request }, testInfo) => {
				test.setTimeout(90_000);
				test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
				skipMismatchedProject(width, testInfo.project.name);
				await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
				await page.emulateMedia({ reducedMotion: reduced ? 'reduce' : 'no-preference' });
				const threadId = await createSession(request, THREAD_TITLE);
				await onlyThread(request, threadId);
				const doc = await seedTextDocument(page, request, threadId, `motion-${width}-${reduced}`, 'launch-plan.md', []);
				await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
				await openThread(page, THREAD_TITLE);
				await expect(chipBox(page)).toHaveCount(0);
				const durations = await page.evaluate(async () => {
					const { ms } = await import('/src/lib/popoverMotion.ts');
					return { standard: ms('--dur-standard'), tight: ms('--dur-tight') };
				});
				if (screenshotDir) writeFileSync(path.join(screenshotDir, `chip-duration-${width}-${reduced ? 'reduced' : 'standard'}.json`), JSON.stringify(durations));
				expect(durations.standard).toBeGreaterThan(0);
				expect(durations.tight).toBeGreaterThan(0);
				await page.waitForTimeout(600);
				addAgentReply(doc.artifactId, doc.roots[8], threadId, 'The owner is confirmed.');
				await startMotionProbe(page);
				await reloadComments(page, doc.artifactId);
				await expectChip(page, '1 new reply');
				const entryEvents = await saveMotionProbe(page, `chip-entry-${width}-${reduced ? 'reduced' : 'standard'}`);
				const entry = entryEvents.filter(event => String(event.target).includes('doc-reply-chip')).flatMap(event => event.animations).find(animation => animation.timing.duration > 0);
				expect(entry?.timing.duration).toBe(reduced ? durations.tight : durations.standard);
				if (reduced) expect(new Set(entry!.keyframes.map(frame => frame.transform)).size).toBe(1);
				await startMotionProbe(page);
				await chipButton(page).click();
				await expect(page.locator(`[data-comment-id="${doc.roots[8]}"]`)).toHaveClass(/focused/);
				await saveMotionProbe(page, `thread-open-${width}-${reduced ? 'reduced' : 'standard'}`);
				await page.waitForTimeout(2000);
				await startMotionProbe(page);
				await page.getByRole('button', { name: 'Close comments' }).click();
				await expect(chipBox(page)).toHaveCount(0);
				await saveMotionProbe(page, `close-button-${width}-${reduced ? 'reduced' : 'standard'}`);
				// Stimulate a second arrival and a viewed event while the document is visible, so the recording includes the chip's own exit.
				addAgentReply(doc.artifactId, doc.roots[8], threadId, 'The handover is ready.');
				await reloadComments(page, doc.artifactId);
				await expectChip(page, '1 new reply');
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
				await expect(chipBox(page)).toHaveCount(0);
				const exitEvents = await saveMotionProbe(page, `chip-exit-${width}-${reduced ? 'reduced' : 'standard'}`);
				const exit = exitEvents.filter(event => String(event.target).includes('doc-reply-chip')).flatMap(event => event.animations).find(animation => animation.timing.duration > 0);
				expect(exit?.timing.duration).toBeCloseTo(durations.standard * 0.7);
				expect(exit?.keyframes.every(frame => frame.transform === undefined)).toBe(true);
			});
		}
	}
});

test.describe('round five composed evidence', () => {
	for (const theme of ['light', 'dark'] as const) {
		test(`${TAG} round5 held Send 393 ${theme}`, async ({ page, request }, info) => {
			test.setTimeout(90_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1' || !info.project.name.endsWith('mobile'));
			await page.setViewportSize({ width: 393, height: 852 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			const doc = await seedTextDocument(page, request, threadId, `r5-send-${theme}`, 'launch-plan.md', [8, 10]);
			await holdDocumentComments(request);
			const from = TEXT.indexOf(SECTIONS[0].anchor);
			await seedHeldComments(request, doc.artifactId, 1, 'Review', { text: SECTIONS[0].anchor, from, to: from + SECTIONS[0].anchor.length });
			await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
			await openThread(page, THREAD_TITLE);
			await expectChip(page, '2 new replies');
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
			const before = await page.screenshot({ scale: 'css', animations: 'disabled' });
			await cdp.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: ['active'] });
			expect(await send.evaluate(node => node.matches(':active'))).toBe(true);
			await page.waitForTimeout(200);
			const activeColor = await send.evaluate(node => getComputedStyle(node).backgroundColor);
			const active = await page.screenshot({ scale: 'css', animations: 'disabled' });
			const md5 = (buffer: Buffer) => createHash('md5').update(buffer).digest('hex');
			if (screenshotDir) {
				const dir = path.join(screenshotDir, 'round5'); mkdirSync(dir, { recursive: true });
				writeFileSync(path.join(dir, `send-before-${theme}-393.png`), before);
				writeFileSync(path.join(dir, `send-active-${theme}-393.png`), active);
				writeFileSync(path.join(dir, `send-${theme}-393.json`), JSON.stringify({ geometry, beforeColor, activeColor, beforeMd5: md5(before), activeMd5: md5(active) }));
			}
			await cdp.send('CSS.forcePseudoState', { nodeId, forcedPseudoClasses: [] }); await cdp.detach();
			expect(activeColor).not.toBe(beforeColor);
			expect(md5(active)).not.toBe(md5(before));
		});
	}

	for (const reduced of [false, true]) {
		test(`${TAG} round5 modal outro 393 ${reduced ? 'reduced' : 'standard'}`, async ({ page, request }, info) => {
			test.setTimeout(90_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1' || !info.project.name.endsWith('mobile'));
			await page.setViewportSize({ width: 393, height: 852 });
			await page.emulateMedia({ reducedMotion: reduced ? 'reduce' : 'no-preference' });
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			const doc = await seedTextDocument(page, request, threadId, `r5-scrim-${reduced}`, 'launch-plan.md', [8, 10]);
			await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
			await openThread(page, THREAD_TITLE);
			await expectChip(page, '2 new replies');
			await page.evaluate(() => {
				const chip = document.querySelector<HTMLElement>('[data-doc-reply-chip]')!;
				const button = chip.querySelector('button')!;
				button.addEventListener('click', () => {
					const rect = chip.getBoundingClientRect(), x = rect.x + rect.width / 2, y = rect.y + rect.height / 2;
					const frames: unknown[] = [];
					(window as unknown as { __r5Frames: unknown[] }).__r5Frames = frames;
					const sample = (time: number) => {
						const current = document.querySelector<HTMLElement>('[data-doc-reply-chip]');
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
			await chipButton(page).click();
			await page.waitForFunction(() => (window as unknown as { __r5Done?: boolean }).__r5Done === true, null, { timeout: 5000 });
			const frames = await page.evaluate(() => (window as unknown as { __r5Frames: Array<{ chipPresent: boolean; scrimPresent: boolean; hitChip: boolean; chipAheadInHitStack: boolean }> }).__r5Frames);
			if (screenshotDir) { const dir = path.join(screenshotDir, 'round5'); mkdirSync(dir, { recursive: true }); writeFileSync(path.join(dir, `modal-outro-393-${reduced ? 'reduced' : 'standard'}.json`), JSON.stringify(frames, null, 2)); }
			expect(frames.some(frame => frame.chipPresent)).toBe(true);
			expect(frames.every(frame => frame.scrimPresent && !frame.hitChip && !frame.chipAheadInHitStack)).toBe(true);
		});
	}
});

test.describe('round six toolbar comparison', () => {
	for (const theme of ['light', 'dark'] as const) {
		test(`${TAG} round6 no held 393 ${theme}`, async ({ page, request }, info) => {
			test.setTimeout(90_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1' || !info.project.name.endsWith('mobile'));
			await page.setViewportSize({ width: 393, height: 852 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			const doc = await seedTextDocument(page, request, threadId, `r6-no-held-${theme}`, 'launch-plan.md', [8, 10]);
			await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
			await openThread(page, THREAD_TITLE);
			await expectChip(page, '2 new replies');
			await expect(page.locator('.document-header .sending-now')).toHaveCount(0);
			await page.mouse.move(391, 426);
			await page.waitForTimeout(600);
			const x = await page.locator('.document-header .comment-status').evaluate(node => node.getBoundingClientRect().x);
			const crop = await page.locator('.document-header').screenshot({ animations: 'disabled', scale: 'css' });
			const version = process.env.R6_VERSION ?? 'candidate';
			if (screenshotDir) {
				const dir = path.join(screenshotDir, 'round6'); mkdirSync(dir, { recursive: true });
				writeFileSync(path.join(dir, `no-held-${version}-${theme}.png`), crop);
				writeFileSync(path.join(dir, `no-held-${version}-${theme}.json`), JSON.stringify({ x, md5: createHash('md5').update(crop).digest('hex') }));
			}
		});
	}
	test(`${TAG} round6 held Send desktop pane 420`, async ({ page, request }, info) => {
		test.setTimeout(90_000);
		test.skip(process.env.CORSIA_E2E_MOCK !== '1' || info.project.name.endsWith('mobile'));
		await page.setViewportSize({ width: 1440, height: 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		const threadId = await createSession(request, THREAD_TITLE);
		await onlyThread(request, threadId);
		const doc = await seedTextDocument(page, request, threadId, 'r6-held-420', 'launch-plan.md', [8, 10]);
		await holdDocumentComments(request);
		const from = TEXT.indexOf(SECTIONS[0].anchor);
		await seedHeldComments(request, doc.artifactId, 1, 'Review', { text: SECTIONS[0].anchor, from, to: from + SECTIONS[0].anchor.length });
		await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
		await openThread(page, THREAD_TITLE);
		await expectChip(page, '2 new replies');
		await page.waitForTimeout(600);
		const geometry = await headerSend(page).evaluate(node => {
			const rect = node.getBoundingClientRect();
			const canvas = node.closest('.document-canvas')!.getBoundingClientRect();
			const hit = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
			return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, canvasX: canvas.x, canvasWidth: canvas.width, whole: rect.x >= canvas.x && rect.right <= canvas.right && rect.y >= canvas.y && rect.bottom <= canvas.bottom, hit: !!hit && (hit === node || node.contains(hit)) };
		});
		await page.mouse.move(1438, 450);
		const crop = await page.locator('.document-header').screenshot({ animations: 'disabled', scale: 'css' });
		if (screenshotDir) {
			const dir = path.join(screenshotDir, 'round6'); mkdirSync(dir, { recursive: true });
			writeFileSync(path.join(dir, 'held-candidate-desktop-420.png'), crop);
			writeFileSync(path.join(dir, 'held-candidate-desktop-420.json'), JSON.stringify({ ...geometry, md5: createHash('md5').update(crop).digest('hex') }));
		}
		expect(geometry.canvasWidth).toBe(420);
		expect(geometry.whole && geometry.hit).toBe(true);
	});
});

// Before frames (light): the same two documents on the base commit, where no chip exists. Run only on the base checkout
// (`--grep baseline`); the assertion that no chip is drawn is what proves the checkout is the base.
for (const width of [1440, 393]) {
	const size = { width, height: width === 393 ? 852 : 900 };

	test(`${TAG} baseline light ${width}px: J1 text document with replies below the fold, no chip`, async ({ page, request }, testInfo) => {
		test.setTimeout(90_000);
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		skipMismatchedProject(width, testInfo.project.name);
		await page.setViewportSize(size);
		await page.emulateMedia({ colorScheme: 'light' });
		const threadId = await createSession(request, THREAD_TITLE);
		await onlyThread(request, threadId);
		const doc = await seedTextDocument(page, request, threadId, `b1-${width}`, 'launch-plan.md', [8, 10]);
		await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
		await openThread(page, THREAD_TITLE);
		await expect(page.locator('.doc .cm-content')).toContainText('Q4 launch plan');
		await expect(chipBox(page)).toHaveCount(0);
		await shot(page, `B1-text-document-before-light-${width}`);
	});

	test(`${TAG} baseline light ${width}px: J2 image document with replies on two pins, no chip`, async ({ page, request }, testInfo) => {
		test.setTimeout(120_000);
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		skipMismatchedProject(width, testInfo.project.name);
		await page.setViewportSize(size);
		await page.emulateMedia({ colorScheme: 'light' });
		seconds = 0;
		await freezeClock(page);
		const seeded = await seedImageDocument(page, request, 'launch-walkthrough', width === 393 ? null : 420);
		await onlyThread(request, seeded.sessionId);
		sqlite([['UPDATE threads SET title=? WHERE id=?', [IMAGE_THREAD_TITLE, seeded.sessionId]]]);
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
		await page.getByText(IMAGE_THREAD_TITLE).first().click();
		await expect(page.locator('.cm-md-image img').first()).toBeVisible();
		// Opening the document delivers the two pin comments; take the frame once the confirmation has come and gone.
		const delivered = page.locator('.toast').filter({ hasText: /comments? sent/ });
		await expect(delivered).toBeVisible();
		await expect(delivered).toHaveCount(0, { timeout: 15_000 });
		await expect(chipBox(page)).toHaveCount(0);
		await shot(page, `B2-image-document-before-light-${width}`);
	});
}
