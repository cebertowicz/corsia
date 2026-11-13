import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { closeComments } from '../support/commentDrawer';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page, type Route } from '@playwright/test';
import { createSession, loadPastFirstRunLanding, loginByUrl, replacePreviousFixtureThread, sidebarThread, TEST_SESSION_TOKEN, TEST_TOKEN } from '../support/corsia';
import { injectMessage } from '../support/mock';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const TAG = '@surface:doc-reply-waiting';

function makeAgentReply(replyId: string, threadId: string): void {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !stateRoot) throw new Error('Requires isolated synthetic state');
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', [
			'import sqlite3, sys',
			'connection = sqlite3.connect(sys.argv[1])',
			'changed = connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Reviewer", sys.argv[3], sys.argv[2])).rowcount',
			'assert changed == 1, changed',
			'connection.commit()',
			'connection.close()',
		].join('\n'), path.join(stateRoot, 'corsia.db'), replyId, threadId,
	], { cwd: repoRoot, encoding: 'utf8' });
}
function seedDecision(threadId: string): void {
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (!python || !workspace || !state || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Requires isolated synthetic state');
	execFileSync(python, [path.join(repoRoot, 'frontend/e2e/support/reply-decision.py'), threadId], {
		cwd: repoRoot,
		env: { ...process.env, PYTHONPATH: 'backend', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state },
	});
}
// `e2e-local` passes its isolated workspace through the narrow environment
// allowlist. Unlike state, it survives each test reset so the run can copy
// its frames into task-owned evidence after Playwright finishes.
const screenshotDir = process.env.REPLY_AWARENESS_SCREENSHOT_DIR ?? process.env.CORSIA_E2E_WORKSPACE;
const EVIDENCE_RUN_MARKER = '.reply-awareness-evidence-run-start';

function prepareEvidenceDirectory(): void {
	if (process.env.REPLY_AWARENESS_REQUIRE_MOBILE_FRAMES !== '1' || !process.env.REPLY_AWARENESS_SCREENSHOT_DIR) return;
	const marker = path.join(screenshotDir, EVIDENCE_RUN_MARKER);
	try {
		writeFileSync(marker, String(Date.now()), { flag: 'wx' });
	} catch (error) {
		if ((error as NodeJS.ErrnoException).code === 'EEXIST') return;
		throw error;
	}
	const staleEntries = readdirSync(screenshotDir).filter((entry) => entry !== EVIDENCE_RUN_MARKER);
	if (staleEntries.length) throw new Error(`Reply-awareness evidence output must start empty; found ${staleEntries.join(', ')}`);
}

test.beforeAll(prepareEvidenceDirectory);
// The stand-in queue reads comments through the test's `request` context. A journey that ends on an action
// reloading the queue leaves that read in flight; let it finish before `request` is torn down under it.
test.afterEach(async ({ page }) => {
	await page.unrouteAll({ behavior: 'wait' });
});

// The people in every frame. The disposable account is called "e2e"; the comments are shown under a real name.
const READER = 'Jordan Lee';
const REPLIER = 'Maya Chen';
const THREAD_TITLE = 'Launch plan review';
// One backend serves every test in the run, so a fixed session title leaves several sidebar rows with the same
// words by the later tests, and opening a thread by title can land on another test's document. Each test titles
// its own session, so a collision fails visibly instead of reading somebody else's state.
const RUN = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
let titleSeq = 0;
let threadTitle = '';
test.beforeEach(() => {
	titleSeq += 1;
	threadTitle = `${THREAD_TITLE} ${RUN}-${titleSeq}`;
});

// Three threads on three different passages, each answered by someone other than the reader.
const PASSAGES = [
	{ anchor: 'The launch needs a single owner.', ask: 'Who owns the launch date?', reply: 'The launch timing and owners now align.' },
	{
		anchor: 'Support coverage for launch week is still undecided.',
		ask: 'Who covers support in launch week?',
		reply: [
			'The on-call rota covers it. I added the daily schedule and the escalation path to the appendix so nobody has to ask twice. Priya and Dev take the first two days and the rest follow the usual rota. If a day is left uncovered I will raise it in the Thursday sync before it becomes a problem.',
			'The handover notes name the backup for every shift, link the escalation guide, and say who confirms coverage each morning. We will review the schedule at the end of each day, record any swap before the next shift starts, and keep the incident commander on the same page as support.',
			'The schedule now records the primary and backup by day, their local coverage hours, and the person who accepts the handover. It also links the escalation guide beside every shift, so the person opening the document can find the next step without leaving the plan. A short daily review confirms the following morning before the previous shift closes.',
			'If coverage changes, the outgoing person writes the reason and tags the replacement before the next shift starts. The support lead checks that entry against the rota, confirms any escalation owner, and raises an unresolved gap in the Thursday sync. This keeps the launch team, the incident commander, and support on one current schedule rather than three slightly different ones.',
		].join(' '),
	},
	{ anchor: 'The contingency budget has not been approved.', ask: 'Is the contingency budget signed off?', reply: 'Finance approved the contingency budget on Thursday. I removed this sentence from the open items and linked the approval email in the appendix. The amount is ten percent of the base plan, and it can only be spent on support cover or on a rollback.' },
] as const;

const LATER_REPLIES = [
	'Dates are locked for the first milestone.',
	'The rota is confirmed for the week of the 31st.',
	'The approval is attached to the budget line.',
] as const;

const DOCUMENT_TEXT = [
	'# Q4 launch plan',
	'',
	'This plan covers the Q4 launch of the new onboarding flow: what ships, who owns it, when it happens and what could go wrong.',
	'',
	'## Goals',
	'',
	'Cut the time from install to first useful result to under ten minutes for a new person. Keep the support queue flat through launch week. Land the announcement with every channel on the same day.',
	'',
	'## Scope',
	'',
	'In scope: the redesigned first-run screens, the guided project setup, and the updated help pages. Out of scope: the mobile app, pricing changes and anything that needs a new integration.',
	'',
	'## Audience',
	'',
	'New people installing on their own machine, most of them trying CORSIA after hearing about it from a colleague. They have a model account already and want a first useful result without reading documentation.',
	'',
	'## Dependencies',
	'',
	'The signing certificate renewal lands on the 10th. The help pages need the final screenshots from design. The announcement needs the release notes frozen two days before launch.',
	'',
	'## Timeline',
	'',
	'Design freeze on the 3rd. Build complete on the 17th. Internal dogfood from the 18th to the 24th. Public launch on the 31st, with a rollback window held open until the following Monday.',
	'',
	'## Ownership',
	'',
	`${PASSAGES[0].anchor} Engineering owns the build, design owns the first-run copy, and support owns the help pages. Each owner reports status in the Thursday sync.`,
	'',
	'## Rollout',
	'',
	'Roll out to ten percent of new installs on day one, then fifty percent on day three, then everyone. Hold at any step if the first-run completion rate drops below the dogfood baseline.',
	'',
	`${PASSAGES[1].anchor} The plan needs a named person for each day and a clear escalation path to engineering.`,
	'',
	'## Budget',
	'',
	`Paid promotion is capped at the amount agreed last quarter. ${PASSAGES[2].anchor} Until it is, no spend beyond the base plan is committed.`,
	'',
	'## Post-launch',
	'',
	'For the first week after launch, the team reviews completion, support volume, and error reports every morning. A named owner records each decision and follows up before the next meeting.',
	'',
	'- Check first-run completion against the dogfood baseline.',
	'- Review the support queue and escalation time.',
	'- Keep the rollback owner available until Monday.',
	'',
	'## Open questions',
	'',
	'Whether the help pages ship with the build or a day later. Whether the announcement needs a localized version at launch. Whether the first-week review should include the partner team.',
	'',
].join('\n');

const FROZEN_NOW = '2026-10-23T15:45:00.000-07:00';
const FROZEN_UTC = new Date(FROZEN_NOW).toISOString();
let seededTimestampOffset = 0;

const CHAT = [
	{ role: 'user', content: 'Can you draft the Q4 launch plan for the onboarding flow?' },
	{ role: 'assistant', content: 'I drafted launch-plan.md: goals, scope, timeline, ownership, rollout and budget. Ownership, support cover and the contingency budget are the three places I would want a second pair of eyes.', metadata: { for_user: true } },
	{ role: 'user', content: 'Thanks. I left a question on each of those three.' },
	{ role: 'assistant', content: 'I have replied to each one in the document. The first answer settles the launch date.', metadata: { for_user: true } },
] as Array<{ role: string; content: string; metadata?: Record<string, unknown> }>;

function skipMismatchedProject(width: number, projectName: string): void {
	const wantsMobile = width === 393;
	test.skip(wantsMobile !== projectName.endsWith('mobile'), 'Each device project captures its native requested viewport.');
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
	}, FROZEN_NOW);
}

// A frame is taken once the page has stopped changing: nothing is loading, and two captures in a row are identical.
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

async function hideDevStamp(page: Page): Promise<void> {
	await page.evaluate(async () => {
		const { coreSettings } = await import('/src/lib/stores/settings.ts');
		coreSettings.update((current) => ({ ...current, features: { ...current?.features, debug_version_overlay: false } }));
	});
	await expect(page.locator('.version-overlay')).toHaveCount(0);
}

async function shot(page: Page, name: string, chatRows = 4): Promise<void> {
	if (!screenshotDir) return;
	await settled(page);
	await hideDevStamp(page);
	// The four seeded chat rows appear in every capture state. Assert their visible timestamps before persisting a frame.
	await expect(page.locator('.msg-time')).toHaveText(Array(chatRows).fill('3:45 PM'));
	// No test identity or fixture wording may be on screen in a frame the reviewer reads as the real product.
	expect(await page.evaluate(() => document.body.innerText), `Frame ${name} shows test wording`).not.toMatch(/\be2e\b|fixture|Another agent|e2e-/i);
	const output = path.join(screenshotDir, `${name}.png`);
	writeFileSync(output, await settled(page));
	expect(existsSync(output), `Saved evidence frame ${name}`).toBeTruthy();
}

// The document is on screen with its text drawn and its comment count read.
async function documentReady(page: Page): Promise<void> {
	await expect(page.locator('.doc .cm-content')).toContainText('Q4 launch plan');
	await expect(page.locator('.doc').getByRole('button', { name: /^\d+ comments?\b/ })).toBeVisible();
}

// The frames belong to the replier's thread, not the conductor landing the app opens on.
async function openThread(page: Page, title: string): Promise<void> {
	if ((page.viewportSize()?.width ?? 0) <= 768) {
		// The first-run landing can close the phone sidebar after it opened (see loadPastFirstRunLanding), so open it again until its row can be pressed.
		await expect(async () => {
			if (!(await page.locator('aside.sidebar.open').count())) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await sidebarThread(page, title).click({ timeout: 2_000 });
		}).toPass({ timeout: 25_000 });
		return;
	}
	await sidebarThread(page, title).click();
}

// Direct rows in the disposable database, as the other fixtures do: each statement is `[sql, params]`.
// A null param receives the frozen time plus a deterministic second offset, so later replies remain newer without changing a frame's visible minute.
function sqlite(statements: Array<[string, Array<string | number | null>]>): void {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !stateRoot) throw new Error('Requires isolated synthetic state');
	const timestampStart = seededTimestampOffset;
	seededTimestampOffset += statements.flatMap(([, params]) => params).filter((value) => value === null).length;
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
		].join('\n'), path.join(stateRoot, 'corsia.db'), JSON.stringify(statements), FROZEN_UTC, String(timestampStart),
	], { cwd: repoRoot, encoding: 'utf8' });
}

// Someone other than the reader answers a thread.
function addAgentReply(artifactId: string, rootId: string, threadId: string, body: string, author = REPLIER): void {
	sqlite([[
		"INSERT INTO doc_comments (id, artifact_id, parent_id, author, author_kind, author_thread_id, body, created_at, updated_at) VALUES (lower(hex(randomblob(16))), ?, ?, ?, 'agent', ?, ?, ?, ?)",
		[artifactId, rootId, author, threadId, body, null, null],
	]]);
}

// Reader follow-ups predate the agent answer. They add realistic context without becoming an unread reply.
function addReaderReply(artifactId: string, rootId: string, body: string): void {
	sqlite([[
		"INSERT INTO doc_comments (id, artifact_id, parent_id, author, author_kind, body, created_at, updated_at) VALUES (lower(hex(randomblob(16))), ?, ?, ?, 'user', ?, ?, ?)",
		[artifactId, rootId, READER, body, null, null],
	]]);
}

async function seedScenario(
	page: Page,
	request: APIRequestContext,
	threadId: string,
	folder: string,
	initialReplyIndexes: readonly number[] = [0, 1, 2],
	options: { name?: string; replier?: string; readerFollowups?: readonly { rootIndex: number; body: string }[] } = {},
): Promise<{ artifactId: string; docPath: string; name: string; roots: string[] }> {
	// Each disposable fixture starts from the same visible minute; offsets only order rows within its own scenario.
	seededTimestampOffset = 0;
	await freezeClock(page);
	const tree = await request.get(`/api/files/tree?token=${encodeURIComponent(TEST_TOKEN)}`);
	const workspace = ((await tree.json()).roots as Array<{ kind: string; path: string }>).find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const name = options.name ?? 'launch-plan.md';
	const docPath = path.join(workspace!, folder, name);
	mkdirSync(path.dirname(docPath), { recursive: true });
	const saved = await request.put(`/api/files/content?token=${encodeURIComponent(TEST_TOKEN)}`, {
		data: { path: docPath, text: DOCUMENT_TEXT, thread_id: threadId, create: true },
	});
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	const roots: string[] = [];
	for (const passage of PASSAGES) {
		const from = DOCUMENT_TEXT.indexOf(passage.anchor);
		expect(from).toBeGreaterThan(-1);
		const root = await request.post(`/api/artifacts/${artifactId}/comments?token=${encodeURIComponent(TEST_TOKEN)}`, {
			data: { body: passage.ask, anchor_text: passage.anchor, from_off: from, to_off: from + passage.anchor.length, delivery_policy: 'immediate' },
		});
		expect(root.ok(), await root.text()).toBeTruthy();
		roots.push((await root.json()).comment.id as string);
	}
	// Reader follow-ups supply height before the agent answer. The card deliberately observes its newest reply,
	// so the unread agent reply must stay newest for the two opened threads to clear once they are opened and closed.
	for (const { rootIndex, body } of options.readerFollowups ?? []) addReaderReply(artifactId, roots[rootIndex], body);
	for (const index of initialReplyIndexes) {
		const passage = PASSAGES[index];
		if (passage) addAgentReply(artifactId, roots[index], threadId, passage.reply, options.replier);
	}

	// The reader has a name, the thread has a conversation, the model reads as a real one, and the rail shows
	// no plan rows for the comments (they were delivered when sent).
	sqlite([
		["UPDATE doc_comments SET author=? WHERE author='e2e'", [READER]],
		["UPDATE models SET display_name='Claude Sonnet 5.5' WHERE id='mock-test-model'", []],
		["UPDATE plan_nodes SET state='closed', archived_at=? WHERE title LIKE '% left % comment%'", [null]],
	]);
	for (const message of CHAT) await injectMessage(request, threadId, message, TEST_TOKEN);
	// The root endpoint uses wall-clock time. Freeze its stamp, but retain the seeded seconds on replies:
	// the viewer's newest-reply contract depends on their deterministic order while their rendered minute stays 3:45 PM.
	sqlite([
		['UPDATE doc_comments SET created_at=?, updated_at=? WHERE artifact_id=? AND parent_id IS NULL', [FROZEN_UTC, FROZEN_UTC, artifactId]],
		['UPDATE messages SET created_at=? WHERE thread_id=?', [FROZEN_UTC, threadId]],
	]);
	return { artifactId, docPath, name, roots };
}

async function reloadComments(page: Page, artifactId: string): Promise<void> {
	await page.evaluate(async (id) => {
		const { loadComments } = await import('/src/lib/stores/docComments.ts');
		await loadComments(id);
	}, artifactId);
}

async function holdViewedStamps(page: Page): Promise<() => Promise<void>> {
	let released = false;
	const pending = new Set<() => void>();
	const inFlight = new Set<Promise<void>>();
	const pattern = '**/api/artifacts/*/comments/*/viewed';
	const handler = (route: Route) => {
		const work = (async () => {
			if (!released) await new Promise<void>((release) => pending.add(release));
			await route.continue();
		})();
		inFlight.add(work);
		void work.then(() => inFlight.delete(work), () => inFlight.delete(work));
		return work;
	};
	await page.route(pattern, handler);
	return async () => {
		released = true;
		for (const release of pending) release();
		await Promise.all(inFlight);
		await page.unroute(pattern, handler);
	};
}

async function stageWorkspace(page: Page, threadId: string, docPath: string, name: string, activeSurface: 'artifact' | 'chat' = 'artifact', includeDocument = true): Promise<void> {
	await page.addInitScript(({ threadId, docPath, name, activeSurface, includeDocument }) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
		threads: { [threadId]: includeDocument
			? { tabs: [{ instanceId: 'reply-waiting', artifactId: null, path: docPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading', landOn: 'new-replies' }], activeTabId: 'reply-waiting', visible: true, activeSurface }
			: { tabs: [], activeTabId: null, visible: false, activeSurface: 'chat' } }, splitWidth: 420,
	})), { threadId, docPath, name, activeSurface, includeDocument });
}

async function stageDocument(page: Page, threadId: string, docPath: string, name: string, width: number): Promise<void> {
	await stageWorkspace(page, threadId, docPath, name);
	await loginByUrl(page);
	if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, threadTitle).click();
}

// On a wide screen the comments open as a sheet while the document canvas is under 900 px. Closing the rail is not
// enough at 1440 (the chat keeps its minimum), so the reader also closes the sidebar and widens the document; the
// comments then sit beside the text. Everything comes back afterwards so the journey carries on unchanged.
async function shotRailClosed(page: Page, name: string, width: number): Promise<void> {
	if (width !== 1440) return;
	const toggle = page.getByRole('button', { name: 'Toggle panel' });
	const rail = page.locator('aside.pane');
	const sidebar = page.locator('aside.sidebar');
	const sidebarToggle = page.getByRole('button', { name: 'Toggle sidebar' });
	await toggle.click();
	await expect(rail).toHaveClass(/collapsed/);
	// The comments drawer can already have taken over the canvas, in which case
	// the sidebar toggle is intentionally absent and the document is already wide.
	const sidebarWasVisible = await sidebarToggle.isVisible().catch(() => false);
	if (sidebarWasVisible) {
		await sidebarToggle.click();
		await expect(sidebar).toHaveClass(/collapsed/);
	}
	const setSplit = (value: number | null) => page.evaluate(async (next) => {
		const { splitWidth } = await import('/src/lib/stores/artifactWorkspace.ts');
		let previous: number | null = null;
		splitWidth.subscribe((current) => { previous = current; })();
		splitWidth.set(next);
		return previous;
	}, value);
	const previous = await setSplit(1000);
	await expect.poll(() => page.locator('.doc').evaluate((element) => element.getBoundingClientRect().width)).toBeGreaterThanOrEqual(900);
	await shot(page, name);
	await setSplit(previous);
	if (sidebarWasVisible) {
		await sidebarToggle.click();
		await expect(sidebar).not.toHaveClass(/collapsed/);
	}
	await toggle.click();
	await expect(rail).not.toHaveClass(/collapsed/);
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		test(`${TAG} ${theme} ${width}px: row and comments pill clear only after thread disclosure`, async ({ page, request }) => {
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			await page.setViewportSize({ width, height: 900 });
			await page.emulateMedia({ colorScheme: theme });
			const title = `reply-waiting-${theme}-${width}-${Date.now()}`;
			const threadId = await createSession(request, title);
			const roots = await request.get(`/api/files/tree?token=${TEST_TOKEN}`);
			const workspace = ((await roots.json()).roots as Array<{ kind: string; path: string }>).find((root) => root.kind === 'workspace')?.path;
			expect(workspace).toEqual(expect.any(String));
			const name = `${title}.md`, docPath = path.join(workspace!, name), text = 'A reply marker belongs on this sentence.\n';
			const saved = await request.put(`/api/files/content?token=${TEST_TOKEN}`, { data: { path: docPath, text, thread_id: threadId, create: true } });
			expect(saved.ok(), await saved.text()).toBeTruthy();
			const artifactId = (await saved.json()).artifact.id as string;
			const root = await request.post(`/api/artifacts/${artifactId}/comments?token=${TEST_TOKEN}`, { data: { body: 'Please review.', anchor_text: 'reply marker', from_off: 2, to_off: 14, delivery_policy: 'immediate' } });
			expect(root.ok(), await root.text()).toBeTruthy();
			const rootId = (await root.json()).comment.id as string;
			const reply = await request.post(`/api/artifacts/${artifactId}/comments?token=${TEST_TOKEN}`, { data: { body: 'Reviewed.', parent_id: rootId, delivery_policy: 'immediate' } });
			expect(reply.ok(), await reply.text()).toBeTruthy();
			makeAgentReply((await reply.json()).comment.id as string, threadId);

			await page.addInitScript(({ threadId, docPath, name }) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
				threads: { [threadId]: { tabs: [{ instanceId: 'reply-waiting', artifactId: null, path: docPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'reply-waiting', visible: true, activeSurface: 'artifact' } }, splitWidth: 420,
			})), { threadId, docPath, name });
			await loginByUrl(page);
			if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await page.getByText(title, { exact: true }).first().click();
			const replyMark = page.locator('.reply-mark');
			const pillUnread = page.locator('.document-header .comment-status');
			if (width === 393) {
				// The artifact row remains mounted in the hidden pane while the document is active.
				await expect(replyMark).toHaveCount(1);
				await expect(pillUnread).toHaveAttribute('aria-label', /1 unread/);
				await page.getByRole('button', { name: 'Toggle panel' }).click();
				await expect(replyMark).toBeVisible();
				await page.getByRole('button', { name: 'Toggle panel' }).click();
			} else {
				await expect(replyMark).toBeVisible();
				await expect(pillUnread).toHaveAttribute('aria-label', /1 unread/);
			}
			// Opening the document alone keeps the passive unread marks intact.
			await expect(pillUnread).toHaveAttribute('aria-label', /1 unread/);
			// Opening the thread from its highlight shows its replies; showing the reply is what stamps it viewed.
			await page.locator('.cm-comment-anchor').first().click();
			await expect(page.getByText('Reviewed.', { exact: true })).toBeVisible();
			await expect(page.locator('.reply-mark')).toHaveCount(0);
			await expect(pillUnread).not.toHaveAttribute('aria-label', /unread/);
		});
	}
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		test(`${TAG} ${theme} ${width}px: arriving in an unread document reveals the first reply and leaves it unread`, async ({ page, request }, testInfo) => {
			test.setTimeout(60_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, threadTitle);
			await replacePreviousFixtureThread(request, threadId);
			const scenario = await seedScenario(page, request, threadId, `arrival-${theme}-${width}`, []);
			await stageWorkspace(page, threadId, scenario.docPath, scenario.name, 'chat', false);
			await loginByUrl(page);
			await expect(sidebarThread(page, threadTitle)).toBeAttached();
			await openThread(page, threadTitle);
			await expect(page.getByText(CHAT[0].content, { exact: true })).toBeVisible();
			await shot(page, `00-current-chat-${theme}-${width}`);
			addAgentReply(scenario.artifactId, scenario.roots[2], threadId, PASSAGES[2].reply);
			const releaseViewedStamps = await holdViewedStamps(page);
			let stamps = 0;
			page.on('request', (req) => { if (req.url().includes('/viewed') && req.method() === 'POST') stamps++; });
			try {
				await page.evaluate(async ({ id, path, title }) => {
					const { openArtifactTab } = await import('/src/lib/stores/artifactWorkspace.ts');
					openArtifactTab(id, { path, title, landOn: 'new-replies' });
				}, { id: threadId, path: scenario.docPath, title: scenario.name });
				// Arrival skips the first two quiet threads and visibly scrolls to Budget's unread reply.
				await expect(page.getByText(PASSAGES[2].reply, { exact: true })).toBeVisible();
				await documentReady(page);
				// Landing reads nothing: marker and pill still agree on one unread reply.
				await expect(page.getByRole('button', { name: '3 comments, 1 unread' })).toBeVisible();
				await expect(page.locator('[data-doc-comments-drawer] .ct-new')).toHaveCount(1);
				await expect.poll(() => page.locator('.cm-scroller').evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
				await shot(page, `01-document-arrival-${theme}-${width}`);
				await shotRailClosed(page, `01-document-arrival-${theme}-${width}-rail-closed`, width);
				expect(stamps, 'landing on the unread reply sends no read stamp').toBe(0);
			} finally {
				await releaseViewedStamps();
			}
		});
	}
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		test(`${TAG} ${theme} ${width}px: later replies remain passive and count in the document pill`, async ({ page, request }, testInfo) => {
			test.setTimeout(60_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, threadTitle);
			await replacePreviousFixtureThread(request, threadId);
			const scenario = await seedScenario(page, request, threadId, `passive-${theme}-${width}`);
			await stageDocument(page, threadId, scenario.docPath, scenario.name, width);
			await expect(page.getByText(PASSAGES[0].reply, { exact: true })).toBeVisible();
			await documentReady(page);
			await closeComments(page);

			// A reply to each thread lands while the reader is in the document: they count, and nothing interrupts.
			await page.waitForTimeout(1100);
			LATER_REPLIES.forEach((body, index) => addAgentReply(scenario.artifactId, scenario.roots[index], threadId, body));
			await reloadComments(page, scenario.artifactId);
			const commentsPill = page.getByRole('button', { name: '3 comments, 3 unread' });
			await expect(commentsPill).toBeVisible();
			await expect(commentsPill).toHaveText(/^\s*3 comments · 3 unread$/);
			await shot(page, `02-later-reply-passive-${theme}-${width}`);

			// Opening the comments shows the three threads the pill counted. Hold the
			// asynchronous viewed acknowledgements until both frames preserve that pre-read state.
			const releaseViewedStamps = await holdViewedStamps(page);
			await page.getByRole('button', { name: '3 comments, 3 unread' }).click();
			await expect(page.locator('[data-doc-comments-drawer] .drawer-heading h2')).toHaveText('Comments 3');
			await expect(page.getByText(LATER_REPLIES[0], { exact: true })).toBeVisible();
			await shot(page, `03-three-replies-${theme}-${width}`);
			await shotRailClosed(page, `03-three-replies-${theme}-${width}-rail-closed`, width);
			await releaseViewedStamps();
		});
	}
}

for (const width of [1440, 393]) {
	test(`${TAG} before state has neither in-document New marker nor chat dock at ${width}px`, async ({ page, request }, testInfo) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		skipMismatchedProject(width, testInfo.project.name);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		const threadId = await createSession(request, threadTitle);
		await replacePreviousFixtureThread(request, threadId);
		const scenario = await seedScenario(page, request, threadId, `before-${width}`, []);
		await stageDocument(page, threadId, scenario.docPath, scenario.name, width);
		await documentReady(page);
		await expect(page.locator('.ct-new')).toHaveCount(0);
		await expect(page.getByRole('button', { name: '3 comments', exact: true })).toBeVisible();
		await shot(page, `00a-before-in-document-light-${width}`);
		await page.evaluate(async (id) => {
			const { closeArtifactTab } = await import('/src/lib/stores/artifactWorkspace.ts');
			closeArtifactTab(id, 'reply-waiting');
		}, threadId);
		await expect(page.getByText(CHAT[0].content, { exact: true })).toBeVisible();
		await expect(page.locator('[data-attention-dock]')).toHaveCount(0);
		await shot(page, `00b-before-chat-light-${width}`);
	});
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 1920, 393]) {
		test(`${TAG} ${theme} ${width}px: transparent controls above the transcript with a decision`, async ({ page, request }, testInfo) => {
			test.setTimeout(60_000);
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, threadTitle);
			await replacePreviousFixtureThread(request, threadId);
			await seedScenario(page, request, threadId, `backdrop-${theme}-${width}`, []);
			const longBubble = Array(80).fill('Please check the launch owners, the release schedule, and the support coverage before the announcement.').join(' ');
			await injectMessage(request, threadId, { role: 'user', content: longBubble }, TEST_TOKEN);
			await injectMessage(request, threadId, { role: 'assistant', content: 'I will review those details.' }, TEST_TOKEN);
			sqlite([['UPDATE messages SET created_at=? WHERE thread_id=?', [FROZEN_UTC, threadId]]]);
			// An existing decision starts collapsed; a newly arriving decision auto-opens.
			seedDecision(threadId);
			await loginByUrl(page);
			await openThread(page, threadTitle);
			const messages = page.locator('.messages');
			await expect(messages.getByText(longBubble)).toBeAttached();
			await messages.locator('.user-bubble-wrapper', { hasText: longBubble }).getByRole('button', { name: 'Show more' }).click();
			await expect.poll(() => messages.evaluate((el) => el.scrollHeight - el.clientHeight)).toBeGreaterThan(200);
			await messages.evaluate((el) => { el.scrollTop = (el.scrollHeight - el.clientHeight) / 2; el.dispatchEvent(new WheelEvent('wheel', { deltaY: -1, bubbles: true })); el.dispatchEvent(new Event('scroll')); });
			const dock = page.locator('.attention-dock');
			await expect(dock).toBeVisible();
			const arrow = page.locator('.chat-attention-stack > .chat-scroll-control');
			await expect(arrow).toBeVisible();
			await expect(page.locator('.chat-scroll-control:visible, .scroll-fab:visible')).toHaveCount(1);
			await expect(dock.locator('.chat-scroll-control')).toHaveCount(0);
			for (const surface of [dock, page.locator('.chat-attention-stack')]) {
				const background = await surface.evaluate(el => ({ color: getComputedStyle(el).backgroundColor, image: getComputedStyle(el).backgroundImage }));
				expect(background).toEqual({ color: 'rgba(0, 0, 0, 0)', image: 'none' });
			}
			if (width === 393) {
				const plan = page.locator('.plan-progress-chip');
				await expect(plan).toBeVisible();
				const arrowBox = (await arrow.boundingBox())!;
				const planBox = (await plan.boundingBox())!;
				expect(Math.abs((arrowBox.y + arrowBox.height / 2) - (planBox.y + planBox.height / 2))).toBeLessThanOrEqual(1);
				expect(arrowBox.width).toBeGreaterThanOrEqual(44);
				expect(planBox.height).toBeGreaterThanOrEqual(44);
			}
			await expect.poll(async () => {
				await page.evaluate(async () => (await import('/src/lib/stores/attentionDock.svelte.ts')).reload());
				return dock.locator('.dock-bar').count();
			}).toBe(1);
			const bar = dock.locator('.dock-bar');
			await expect(bar).not.toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
			await bar.focus();
			await expect(bar).not.toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
			await bar.evaluate(el => el.blur());
			if (width !== 393) {
				await bar.hover();
				await expect(bar).not.toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
				await page.mouse.move(0, 0);
			}
			await shot(page, `12-backdrop-needs-you-${theme}-${width}`, 5);
			if (width === 393) {
				await bar.click();
				const decisionDrawer = page.getByRole('dialog', { name: /^Decision —/ });
				await expect(decisionDrawer).toBeVisible();
				await shot(page, `13-decision-expanded-${theme}-${width}`, 5);
				await page.keyboard.press('Escape');
				await expect(decisionDrawer).toBeHidden();
				await page.locator('.plan-progress-chip').click();
				const drawer = page.getByRole('dialog', { name: /^Plan —/ });
				await expect(drawer).toBeVisible();
				await drawer.getByRole('button', { name: 'Done' }).click();
				await expect(drawer).toBeHidden();
			}
			await arrow.click();
			await expect.poll(() => messages.evaluate(el => el.scrollHeight - el.clientHeight - el.scrollTop)).toBeLessThan(3);
			const queue = await request.get('/api/attention/queue', { params: { token: TEST_TOKEN } });
			const decision = (await queue.json()).current.detail;
			const answer = await request.post(`/api/plans/decisions/${decision.id}/answer`, {
				params: { token: TEST_SESSION_TOKEN }, data: { option_id: 'A', expected_revision: decision.revision, mutation_id: `answer-${threadId}` },
			});
			expect(answer.ok(), await answer.text()).toBeTruthy();
		});
	}
}

test.afterAll(({}, testInfo) => {
	// This completeness contract belongs to the deliberate full evidence run.
	// Focused diagnosis runs select one scenario and must not be made to emit
	// unrelated frames just to satisfy it. The mobile project runs last, after
	// both responsive projects have written the whole evidence set.
	if (process.env.REPLY_AWARENESS_REQUIRE_MOBILE_FRAMES !== '1' || !screenshotDir || !testInfo.project.name.endsWith('mobile')) return;

	const expected = new Set<string>();
	for (const theme of ['light', 'dark']) {
		for (const width of [1440, 393]) {
			for (const frame of [
				'00-current-chat',
				'01-document-arrival',
				'02-later-reply-passive',
				'03-three-replies',
				'12-backdrop-needs-you',
			]) expected.add(`${frame}-${theme}-${width}.png`);
			if (width === 1440) {
				expected.add(`01-document-arrival-${theme}-${width}-rail-closed.png`);
				expected.add(`03-three-replies-${theme}-${width}-rail-closed.png`);
			}
		}
	}
	for (const width of [1440, 393]) {
		for (const frame of [
			'00a-before-in-document',
			'00b-before-chat',
		]) expected.add(`${frame}-light-${width}.png`);
	}

	const runStartedAt = statSync(path.join(screenshotDir, EVIDENCE_RUN_MARKER)).mtimeMs;
	const capturedFrames = readdirSync(screenshotDir).filter((entry) => entry.endsWith('.png')).sort();
	expect(capturedFrames, 'The empty evidence directory contains exactly the current run’s frames').toEqual([...expected].sort());
	for (const frame of expected) {
		const output = path.join(screenshotDir, frame);
		expect(existsSync(output), `Persisted reply-awareness evidence frame ${output}`).toBeTruthy();
		expect(statSync(output).mtimeMs, `Evidence frame ${output} was written after this run started`).toBeGreaterThanOrEqual(runStartedAt);
	}
});

export { TAG };
