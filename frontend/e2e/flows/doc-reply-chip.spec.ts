import { execFileSync } from 'node:child_process';
import { mkdirSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { chatComposer, createSession, loginByUrl, sidebarThread, TEST_TOKEN } from '../support/corsia';
import { injectMessage } from '../support/mock';
import { DOC, DOC_IMAGE_NATURAL, overlay, putSheetAway, seedImageDocument } from '../support/docImageComments';
import { headerSend, seedHeldComments } from '../support/heldComments';

// Evidence frames for the document reply chip (R5): the chat's own "N new replies" chip, floated over the document it counts.
// Mock-mode only: every state is seeded straight into the disposable database, and the clock is frozen so a frame is repeatable.

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

/** The chip, once drawn: its text, and that nothing else on the document's surface sits under it. */
async function expectChip(page: Page, text: string): Promise<void> {
	await expect(chipButton(page)).toHaveText(new RegExp(text));
	const chip = (await chipBox(page).boundingBox())!;
	const view = page.viewportSize()!;
	expect(chip.x, 'inside the viewport on the left').toBeGreaterThanOrEqual(0);
	expect(chip.x + chip.width, 'inside the viewport on the right').toBeLessThanOrEqual(view.width + 0.5);
	expect(chip.y + chip.height, 'inside the viewport at the bottom').toBeLessThanOrEqual(view.height + 0.5);
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
			await seedHeldComments(request, doc.artifactId, 1, 'Review', { text: SECTIONS[0].anchor, from: heldFrom, to: heldFrom + SECTIONS[0].anchor.length });
			tidy(doc.artifactId);
			await stageWorkspace(page, threadId, doc.docPath, 'launch-plan.md', 'artifact');
			await openThread(page, THREAD_TITLE);
			await expect(page.locator('.doc .cm-content')).toContainText('Q4 launch plan');
			// Opened at the top: both replies are below the fold, the document says so once, in the chat's own chip.
			await expectChip(page, '2 new replies');
			await expect(headerSend(page)).toBeVisible();
			const sendBox = (await headerSend(page).boundingBox())!;
			const replyBox = (await chipButton(page).boundingBox())!;
			expect(replyBox.x < sendBox.x + sendBox.width && sendBox.x < replyBox.x + replyBox.width && replyBox.y < sendBox.y + sendBox.height && sendBox.y < replyBox.y + replyBox.height, 'held-comment Send stays clear of the reply target').toBe(false);
			await shot(page, `01d-held-send-with-chip-${theme}-${width}`);
			await expect(chipBox(page).locator('.ct-new')).toBeVisible();
			// Probe the actual hit target at both vertical edges, outside the visual chip face.
			const edgeHits = await chipButton(page).evaluate((button) => {
				const box = button.getBoundingClientRect();
				const x = box.left + box.width / 2;
				const center = box.top + box.height / 2;
				return [-21, 21].map((offset) => button.contains(document.elementFromPoint(x, center + offset)));
			});
			expect(edgeHits).toEqual([true, true]);
			await shot(page, `01-text-document-chip-${theme}-${width}`);
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
				return { y: rect.y, height: rect.height };
			});
			expect(caretBox.height, 'caret has visible geometry').toBeGreaterThan(0);
			const editorAfter = (await page.locator('.doc-main').boundingBox())!;
			const chipAfter = (await chipBox(page).boundingBox())!;
			expect(caretBox.y + caretBox.height, 'caret stays inside the editor').toBeLessThanOrEqual(editorAfter.y + editorAfter.height + 0.5);
			expect(caretBox.y + caretBox.height, 'caret stays above the reply chip').toBeLessThanOrEqual(chipAfter.y);
			await shot(page, `01c-visible-caret-with-chip-${theme}-${width}`);
			await page.locator('.cm-scroller').evaluate((el) => { el.scrollTop = 0; });
			const before = await page.locator('.cm-scroller').evaluate((el) => el.scrollTop);
			await holdDwell(page, true);
			await chipButton(page).click();
			// The first waiting reply is brought into view, focused, and the comments list opens at it.
			await expect(page.locator(`[data-comment-id="${doc.roots[8]}"]`)).toHaveClass(/focused/);
			await expect.poll(() => page.locator('.cm-scroller').evaluate((el) => el.scrollTop)).toBeGreaterThan(before);
			await shot(page, `02-text-document-landed-${theme}-${width}`);
			await holdDwell(page, false);
		});
	}
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
			await chipButton(page).click();
			// The image opens with the pin's thread open, its reply marked New; the chip steps aside while the image covers the document.
			await expect(overlay(page)).toBeVisible();
			await expect(page.getByText('Yes, the header is final. I attached the export.')).toBeVisible();
			await expect(chipBox(page)).toHaveCount(0);
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
