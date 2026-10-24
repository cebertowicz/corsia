import { execFileSync } from 'node:child_process';
import { existsSync, mkdirSync, readdirSync, rmSync, statSync, writeFileSync } from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page, type Route } from '@playwright/test';
import { chatComposer, createSession, loadPastFirstRunLanding, loginByUrl, sidebarThread, TEST_SESSION_TOKEN, TEST_TOKEN } from '../support/corsia';
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
const LONG_REPLIER = 'Maya Chen — Customer Support Operations';
const THREAD_TITLE = 'Launch plan review';
const LONG_DOCUMENT_NAME = 'launch-plan-for-the-Q4-onboarding-and-support-readiness-review.md';

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

// Reader context that keeps the third unread reply below the comments sheet once the first two have been on screen.
function partialReadFollowups(width: number): Array<{ rootIndex: number; body: string }> {
	return width === 393
		? [{ rootIndex: 0, body: 'Noted.' }]
		: [
			{ rootIndex: 0, body: 'I checked the owner list against the release calendar and noted the two dependencies that must be confirmed before the launch date can hold. The rest of the work is already assigned, so this is only a request for confirmation rather than a new scope decision.' },
			{ rootIndex: 0, body: 'Noted.' },
		];
}

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

async function shot(page: Page, name: string): Promise<void> {
	if (!screenshotDir) return;
	await settled(page);
	await hideDevStamp(page);
	// The four seeded chat rows appear in every capture state. Assert their visible timestamps before persisting a frame.
	await expect(page.locator('.msg-time')).toHaveText(['3:45 PM', '3:45 PM', '3:45 PM', '3:45 PM']);
	// No test identity or fixture wording may be on screen in a frame the reviewer reads as the real product.
	expect(await page.evaluate(() => document.body.innerText), `Frame ${name} shows test wording`).not.toMatch(/\be2e\b|fixture|Another agent|e2e-/i);
	const output = path.join(screenshotDir, `${name}.png`);
	writeFileSync(output, await settled(page));
	expect(existsSync(output), `Saved evidence frame ${name}`).toBeTruthy();
}

async function tabTo(page: Page, selector: string): Promise<void> {
	for (let index = 0; index < 60; index += 1) {
		await page.keyboard.press('Tab');
		if (await page.locator(selector).evaluate((element) => document.activeElement === element)) return;
	}
	throw new Error(`Keyboard focus did not reach ${selector}`);
}

// The document is on screen with its text drawn and its comment count read.
async function documentReady(page: Page): Promise<void> {
	await expect(page.locator('.doc .cm-content')).toContainText('Q4 launch plan');
	await expect(page.locator('.doc').getByRole('button', { name: /open threads?/ })).toBeVisible();
}

// Frames show one clean fixture: the other tests' sessions would otherwise crowd the sidebar.
async function onlyThread(request: APIRequestContext, keepId: string): Promise<void> {
	const listed = await (await request.get(`/api/threads?token=${TEST_TOKEN}`)).json();
	for (const thread of (listed.threads ?? []) as Array<{ id: string }>) {
		if (thread.id !== keepId) await request.delete(`/api/threads/${thread.id}?token=${TEST_TOKEN}`);
	}
}

// The frames belong to the replier's thread, not the conductor landing the app opens on.
async function openThread(page: Page, title: string): Promise<void> {
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
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
	// so the unread agent reply must stay newest for the two opened threads to clear after their visible dwell.
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

/** A second real registered document for the cross-document aggregation fixture. */
/*
async function seedAdditionalReplyDocumentBroken(
	request: APIRequestContext,
	first: { docPath: string },
	threadId: string,
): Promise<{ artifactId: string; docPath: string; name: string; roots: string[] }> {
	const name = 'release-notes.md';
	const docPath = path.join(path.dirname(first.docPath), name);
	const saved = await request.put(`/api/files/content?token=[REDACTED]encodeURIComponent(TEST_TOKEN)}`, {
		data: { path: docPath, text: DOCUMENT_TEXT, thread_id: threadId, create: true },
	});
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	const passage = PASSAGES[0];
	const from = DOCUMENT_TEXT.indexOf(passage.anchor);
	const root = await request.post(`/api/artifacts/${artifactId}/comments?token=[REDACTED]encodeURIComponent(TEST_TOKEN)}`, {
		data: { body: passage.ask, anchor_text: passage.anchor, from_off: from, to_off: from + passage.anchor.length, delivery_policy: 'immediate' },
	});
	expect(root.ok(), await root.text()).toBeTruthy();
	const rootId = (await root.json()).comment.id as string;
	addAgentReply(artifactId, rootId, threadId, 'The release notes now include the confirmed launch timing.');
	sqlite([
		['UPDATE doc_comments SET author=?, created_at=?, updated_at=? WHERE id=?', [READER, FROZEN_UTC, FROZEN_UTC, rootId]],
	]);
	return { artifactId, docPath, name, roots: [rootId] };
}

*/
async function seedAdditionalReplyDocument(
	request: APIRequestContext,
	first: { docPath: string },
	threadId: string,
): Promise<{ artifactId: string; docPath: string; name: string; roots: string[] }> {
	const name = 'release-notes.md';
	const docPath = path.join(path.dirname(first.docPath), name);
	const saved = await request.put('/api/files/content', {
		params: { token: TEST_TOKEN },
		data: { path: docPath, text: DOCUMENT_TEXT, thread_id: threadId, create: true },
	});
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	const passage = PASSAGES[0];
	const from = DOCUMENT_TEXT.indexOf(passage.anchor);
	const root = await request.post(`/api/artifacts/${artifactId}/comments`, {
		params: { token: TEST_TOKEN },
		data: { body: passage.ask, anchor_text: passage.anchor, from_off: from, to_off: from + passage.anchor.length, delivery_policy: 'immediate' },
	});
	expect(root.ok(), await root.text()).toBeTruthy();
	const rootId = (await root.json()).comment.id as string;
	addAgentReply(artifactId, rootId, threadId, 'The release notes now include the confirmed launch timing.');
	sqlite([
		['UPDATE doc_comments SET author=?, created_at=?, updated_at=? WHERE id=?', [READER, FROZEN_UTC, FROZEN_UTC, rootId]],
		// Posting the comment raised a plan row for it; seedScenario closes the first document's rows the same way.
		["UPDATE plan_nodes SET state='closed', archived_at=? WHERE title LIKE '% left % comment%'", [null]],
	]);
	return { artifactId, docPath, name, roots: [rootId] };
}

// Stands in for the queue the backend will serve: one item per open thread with a reply the reader has not seen,
// in document order, read from the real comment state, so the dock advances when the document stamps a reply seen.
async function serveReplyQueue(
	page: Page,
	request: APIRequestContext,
	scenario: { artifactId: string; docPath: string; name: string },
	threadId: string,
	documentAvailable: () => boolean = () => true,
	includeNeedsYou = false,
	additionalScenarios: readonly { artifactId: string; docPath: string; name: string }[] = [],
): Promise<void> {
	await page.route('**/api/attention/queue', async (route) => {
		if (!documentAvailable()) {
			await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ current: null, count: 0, items: [] }) });
			return;
		}
		const listed = await (await request.get(`/api/artifacts/${scenario.artifactId}/comments?token=${encodeURIComponent(TEST_TOKEN)}`)).json();
		type Thread = { id: string; anchor_text: string | null; from_off: number | null; new_reply_ids?: string[]; replies?: Array<{ id: string; body: string; author: string; created_at: string }> };
		const documents = [
			{ document: scenario, comments: listed.comments as Thread[] },
			...(await Promise.all(additionalScenarios.map(async (document) => ({
				document,
				comments: (await (await request.get(`/api/artifacts/${document.artifactId}/comments?${new URLSearchParams({ token: TEST_TOKEN })}`)).json()).comments as Thread[],
			})))),
		];
		const waiting = documents
			.flatMap(({ document, comments }) => comments
				.filter((thread) => (thread.new_reply_ids?.length ?? 0) > 0)
				.map((thread) => ({ document, thread })))
			.sort((left, right) => (left.thread.from_off ?? 0) - (right.thread.from_off ?? 0));
		const items = waiting.map(({ document, thread }, index) => {
			const reply = thread.replies!.find((candidate) => candidate.id === thread.new_reply_ids!.at(-1))!;
			return {
				external_id: `reply-${thread.id}`,
				renderer: 'comment-reply',
				detail: {
					id: `reply-${thread.id}`,
					title: document.name,
					excerpt: reply.body,
					quoted_text: thread.anchor_text ?? undefined,
					position: index + 1,
					total_count: waiting.length,
					unread_count: thread.new_reply_ids!.length,
					newest_at: reply.created_at,
					destination: { pane: 'document-replies', id: document.artifactId, path: document.docPath, title: document.name },
					source: { thread_id: threadId, thread_label: reply.author, project_id: null, project_label: null, parent_reviewed: true },
				},
			};
		});
		const queueItems = includeNeedsYou
			? [{
				external_id: 'fixture-decision', renderer: 'decision', detail: {
					id: 'fixture-decision', plan_id: 'fixture-plan', node_id: 'fixture-node', source_node_id: 'fixture-node',
					thread_id: 'release-review', title: 'Confirm launch timing', context: '', tradeoff: '', recommendation: 'yes',
					options: [{ id: 'yes', label: 'Yes', detail: '' }], other_enabled: false, order_key: 0, revision: 1,
					raised_at: null, source: { thread_id: 'release-review', thread_label: 'Release coordinator', project_id: null, project_label: null, parent_reviewed: true },
				},
			}, ...items]
			: items;
		await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ current: queueItems[0] ?? null, count: queueItems.length, items: queueItems }) });
	});
}

async function reloadComments(page: Page, artifactId: string): Promise<void> {
	await page.evaluate(async (id) => {
		const { loadComments } = await import('/src/lib/stores/docComments.ts');
		await loadComments(id);
	}, artifactId);
}

// Frame 03 captures the deterministic unread state before its visible cards acknowledge those replies.
async function resetDockPosition(page: Page): Promise<void> {
	await page.evaluate(async () => {
		const { activeId, expandedId } = await import('/src/lib/stores/attentionDock.svelte.ts');
		activeId.set(null);
		expandedId.set(null);
	});
}

async function holdViewedStamps(page: Page): Promise<() => Promise<void>> {
	const pending = new Set<() => void>();
	const pattern = '**/api/artifacts/*/comments/*/viewed';
	const handler = async (route: Route) => {
		await new Promise<void>((release) => pending.add(release));
		await route.continue();
	};
	await page.route(pattern, handler);
	return async () => {
		for (const release of pending) release();
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
	await page.getByText(THREAD_TITLE, { exact: true }).first().click();
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

// Top-to-bottom order on screen: every box is fully in the viewport and ends at or above the next one's top.
// Returns the problems found, so a failed poll names the overlapping or clipped pieces.
async function stackProblems(page: Page, parts: Record<string, ReturnType<Page['locator']>>): Promise<string[]> {
	const viewport = page.viewportSize()!;
	const named = await Promise.all(Object.entries(parts).map(async ([name, locator]) => [name, await locator.boundingBox()] as const));
	const problems: string[] = [];
	for (const [index, [name, box]] of named.entries()) {
		if (!box) { problems.push(`${name} has no box`); continue; }
		const edges = `${Math.round(box.x)},${Math.round(box.y)} ${Math.round(box.width)}x${Math.round(box.height)}`;
		if (box.x < 0 || box.y < 0 || box.x + box.width > viewport.width + 0.5 || box.y + box.height > viewport.height + 0.5) problems.push(`${name} ${edges} leaves the viewport`);
		const [previousName, previous] = named[index - 1] ?? [];
		if (previous && previous.y + previous.height > box.y + 0.5) problems.push(`${previousName} overlaps ${name} ${edges}`);
	}
	return problems;
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		test(`${TAG} ${theme} ${width}px: row and margin markers clear only after thread disclosure`, async ({ page, request }) => {
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
			const marginReply = page.locator('.cm-comment-marker svg circle');
			if (width === 393) {
				// The artifact row remains mounted in the hidden pane while the document is active.
				await expect(replyMark).toHaveCount(1);
				await expect(marginReply).toBeVisible();
				await page.getByRole('button', { name: 'Toggle panel' }).click();
				await expect(replyMark).toBeVisible();
				await page.getByRole('button', { name: 'Toggle panel' }).click();
			} else {
				await expect(replyMark).toBeVisible();
				await expect(marginReply).toBeVisible();
			}
			// Opening the document alone keeps the passive markers intact.
			await expect(marginReply).toBeVisible();
			// Opening the thread from its marker shows its replies; showing the reply is what stamps it viewed.
			await page.locator('.cm-comment-marker').first().press('Enter');
			await expect(page.getByText('Reviewed.', { exact: true })).toBeVisible();
			await expect(page.locator('.reply-mark')).toHaveCount(0);
			await expect(page.locator('.cm-comment-marker svg circle')).toHaveCount(0);
		});

		test(`${TAG} ${theme} ${width}px: reply chip stacks above Needs you and the composer`, async ({ page, request }, testInfo) => {
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			await seedScenario(page, request, threadId, `stacked-needs-you-${theme}-${width}`, [0]);
			seedDecision(threadId);
			const queue = await request.get('/api/attention/queue', { params: { token: TEST_TOKEN } });
			expect(queue.ok()).toBeTruthy();
			const projected = await queue.json();
			expect(projected.current.renderer).toBe('decision');
			expect(projected.current.detail.source.thread_id).toBe(threadId);
			expect(projected.reply_items.filter((item: { detail: { source: { thread_id: string } } }) => item.detail.source.thread_id === threadId)).toHaveLength(1);
			await loginByUrl(page);
			await openThread(page, THREAD_TITLE);
			const chip = page.locator('.reply-chip-notice');
			const dock = page.locator('[data-attention-dock]');
			const composer = chatComposer(page);
			await expect(chip).toBeVisible();
			// The chip is the shared AttachmentRow chip and counts only replies; the decision stays the dock's one job.
			await expect(chip.getByRole('button')).toContainText('1 new reply · launch-plan.md');
			await expect(dock).toBeVisible();
			await expect(dock).toContainText('Choose the launch approach');
			await expect(dock).not.toContainText(' of ');
			await expect(composer).toBeVisible();
			await expect.poll(() => stackProblems(page, { chip, dock, composer })).toEqual([]);
			await shot(page, `03a-reply-chip-needs-you-stack-${theme}-${width}`);
			if (width === 393) {
				// The transcript outgrows the phone screen, so the frame above is scrolled to the newest message and the oldest rows
				// pass under the header, as in any long chat. Prove nothing is lost: scrolled to the top, the first message is
				// the topmost element at its own top edge, not covered by the header.
				await page.locator('.messages').evaluate((element) => { element.scrollTop = 0; });
				await expect.poll(() => page.locator('.messages').evaluate((element) => element.scrollTop)).toBe(0);
				const firstMessageReachable = await page.locator('.messages .msg-time').first().evaluate((time) => {
					const bubble = time.closest('.messages > *') ?? time;
					const box = bubble.getBoundingClientRect();
					const hit = document.elementFromPoint(box.left + box.width / 2, box.top + 4);
					return !!hit && bubble.contains(hit);
				});
				expect(firstMessageReachable, 'The first message is clear of the header when the transcript is scrolled to the top').toBe(true);
			}
			const decision = projected.current.detail;
			const answer = await request.post(`/api/plans/decisions/${decision.id}/answer`, {
				params: { token: TEST_SESSION_TOKEN }, data: { option_id: 'A', expected_revision: decision.revision, mutation_id: `answer-${threadId}` },
			});
			expect(answer.ok(), await answer.text()).toBeTruthy();
		});
	}
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		test(`${TAG} ${theme} ${width}px: one chip aggregates two documents and opens the newest`, async ({ page, request }, testInfo) => {
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			const first = await seedScenario(page, request, threadId, `two-documents-${theme}-${width}`, [0]);
			// SQLite timestamps have second precision. The later document must have a strictly newer reply so
			// this checks R4.2's cross-document destination rule rather than relying on a tie in server order.
			await page.waitForTimeout(1_100);
			const newest = await seedAdditionalReplyDocument(request, first, threadId);
			// Use the real queue projection for cross-document aggregation.
			await loginByUrl(page);
			await openThread(page, THREAD_TITLE);
			const notice = page.locator('.reply-chip-notice');
			const chip = notice.getByRole('button');
			await expect(chip).toContainText('2 new replies · 2 documents');
			await expect(page.locator('[data-attention-dock]')).toHaveCount(0);
			await expect(chatComposer(page)).toBeVisible();
			await expect.poll(() => stackProblems(page, { chip: notice, composer: chatComposer(page) })).toEqual([]);
			await shot(page, `03b-reply-chip-two-documents-${theme}-${width}`);
			await chip.click();
			await documentReady(page);
			await expect(page.locator(`[data-comment-id="${newest.roots[0]}"]`)).toHaveClass(/focused/);
		});
	}
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		test(`${TAG} ${theme} ${width}px: arriving in an unread document reveals and stamps the first reply`, async ({ page, request }, testInfo) => {
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			const scenario = await seedScenario(page, request, threadId, `arrival-${theme}-${width}`, [2]);
			await stageWorkspace(page, threadId, scenario.docPath, scenario.name, 'chat', false);
			await loginByUrl(page);
			await expect(sidebarThread(page, THREAD_TITLE)).toBeAttached();
			await openThread(page, THREAD_TITLE);
			await expect(page.getByText(CHAT[0].content, { exact: true })).toBeVisible();
			await expect(page.locator('.reply-chip-notice')).toHaveCount(0);
			await shot(page, `00-current-chat-${theme}-${width}`);
			await serveReplyQueue(page, request, scenario, threadId);
			// The reply is now waiting: the backend would push the queue, so the stand-in queue is read again.
			await page.evaluate(async () => (await import('/src/lib/stores/attentionDock.svelte.ts')).reload());
			await expect(page.locator('.reply-chip-notice').getByRole('button')).toContainText('1 new reply · launch-plan.md');
			const releaseViewedStamps = await holdViewedStamps(page);
			await page.evaluate(async ({ id, path, title }) => {
				const { openArtifactTab } = await import('/src/lib/stores/artifactWorkspace.ts');
				openArtifactTab(id, { path, title, landOn: 'new-replies' });
			}, { id: threadId, path: scenario.docPath, title: scenario.name });
			// Arrival skips the first two quiet threads and visibly scrolls to Budget's unread reply.
			await expect(page.getByText(PASSAGES[2].reply, { exact: true })).toBeVisible();
			await documentReady(page);
			// This landing frame is intentionally before the 1.5 s visible dwell: marker, pill, and dock still agree.
			await expect(page.getByRole('button', { name: '3 open threads, 1 unread' })).toBeVisible();
			await expect(page.locator('.ct-new')).toHaveCount(1);
			// The reply is on screen but not yet read (the stamp is held), so Chat still counts it beside the document.
			if (width === 1440) await expect(page.locator('.reply-chip-notice')).toContainText('1 new reply · launch-plan.md');
			await expect.poll(() => page.locator('.cm-scroller').evaluate((element) => element.scrollTop)).toBeGreaterThan(0);
			await shot(page, `01-document-arrival-${theme}-${width}`);
			await shotRailClosed(page, `01-document-arrival-${theme}-${width}-rail-closed`, width);
			await releaseViewedStamps();
		});
	}
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		test(`${TAG} ${theme} ${width}px: later replies remain passive and count in the document pill`, async ({ page, request }, testInfo) => {
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			const scenario = await seedScenario(page, request, threadId, `passive-${theme}-${width}`);
			await serveReplyQueue(page, request, scenario, threadId);
			await stageDocument(page, threadId, scenario.docPath, scenario.name, width);
			await expect(page.getByText(PASSAGES[0].reply, { exact: true })).toBeVisible();
			await documentReady(page);
			await page.getByRole('button', { name: 'Close comments' }).click();

			// A reply to each thread lands while the reader is in the document: they count, and nothing interrupts.
			await page.waitForTimeout(1100);
			LATER_REPLIES.forEach((body, index) => addAgentReply(scenario.artifactId, scenario.roots[index], threadId, body));
			await reloadComments(page, scenario.artifactId);
			// The backend would push the new queue; the stand-in queue is read again.
			await page.evaluate(async () => (await import('/src/lib/stores/attentionDock.svelte.ts')).reload());
			await resetDockPosition(page);
			const commentsPill = page.getByRole('button', { name: '3 open threads, 3 unread' });
			await expect(commentsPill).toBeVisible();
			await expect(commentsPill).toHaveText(/^\s*3 threads · 3 unread$/);
			// Beside the document, Chat counts the same waiting replies in its one chip; none enters the Needs-you dock.
			// The pill counts the three unread threads; the chip counts their replies: each seeded reply, closed before
			// its dwell stamped it, plus the later one.
			if (width === 1440) {
				await expect(page.locator('.reply-chip-notice').getByRole('button')).toContainText('6 new replies · launch-plan.md');
				await expect(page.locator('[data-attention-dock]')).toHaveCount(0);
			}
			await shot(page, `02-later-reply-passive-${theme}-${width}`);

			// Opening the comments shows the three threads the pill and the dock counted. Hold the
			// asynchronous viewed acknowledgements until both frames preserve that pre-read state.
			const releaseViewedStamps = await holdViewedStamps(page);
			await page.getByRole('button', { name: '3 open threads, 3 unread' }).click();
			await expect(page.getByText('3 open', { exact: true })).toBeVisible();
			await expect(page.getByText(LATER_REPLIES[0], { exact: true })).toBeVisible();
			// Beside the document, Chat counts the same waiting replies in its one chip; none enters the Needs-you dock.
			// The pill counts the three unread threads; the chip counts their replies: each seeded reply, closed before
			// its dwell stamped it, plus the later one.
			if (width === 1440) {
				await expect(page.locator('.reply-chip-notice').getByRole('button')).toContainText('6 new replies · launch-plan.md');
				await expect(page.locator('[data-attention-dock]')).toHaveCount(0);
			}
			await shot(page, `03-three-replies-${theme}-${width}`);
			await shotRailClosed(page, `03-three-replies-${theme}-${width}-rail-closed`, width);
			await releaseViewedStamps();
		});
	}
}

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		test(`${TAG} ${theme} ${width}px: persistent reply chip lands on the first unread document reply`, async ({ page, request }, testInfo) => {
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			const scenario = await seedScenario(page, request, threadId, `${theme}-${width}`, [0, 1, 2], {
				readerFollowups: partialReadFollowups(width),
			});
			// The dock belongs on Chat. Keep this journey there until its explicit
			// destination action opens the seeded document.
			await loginByUrl(page);
			await openThread(page, THREAD_TITLE);
			const replyChip = page.locator('.reply-chip-notice');
			await expect(replyChip).toBeVisible();
			await expect(replyChip.getByRole('button')).toContainText('3 new replies · launch-plan.md');
			await expect(page.locator('[data-attention-dock]')).toHaveCount(0);
			await shot(page, `04-reply-chip-${theme}-${width}`);
			await replyChip.getByRole('button').click();
			await documentReady(page);
			await expect(page.locator(`[data-comment-id="${scenario.roots[0]}"]`)).toHaveClass(/focused/);
			// The first two visible cards clear together after the dwell; the long third card stays below the sheet viewport.
			await page.waitForTimeout(1_700);
			await expect(page.getByRole('button', { name: '3 open threads, 1 unread', exact: true })).toBeVisible();
			await expect(page.locator('[data-doc-comments-drawer] .ct-new')).toHaveCount(1);
			// The remaining New reply must be wholly below the sheet viewport, not merely clipped at its edge.
			const unreadCard = page.locator(`[data-comment-id="${scenario.roots[2]}"]`);
			// The reader context keeps this agent reply beyond the viewport without changing the unread-thread count.
			await expect.poll(() => unreadCard.evaluate((card) => {
				const viewport = card.closest('[data-doc-comments-drawer]')?.querySelector<HTMLElement>('.drawer-comments');
				if (!viewport) return false;
				return card.getBoundingClientRect().top >= viewport.getBoundingClientRect().bottom;
			})).toBe(true);
			// The document keeps the unread reply until its own visible dwell stamps it.
			await expect(page.getByText(PASSAGES[0].reply, { exact: true }).last()).toBeVisible();
			if (width === 1440) await expect(page.locator('.reply-chip-notice')).toContainText('1 new reply · launch-plan.md');
			await shot(page, `05-open-in-document-${theme}-${width}`);
			await shotRailClosed(page, `05-open-in-document-${theme}-${width}-rail-closed`, width);
			// What the dock reads next, on Chat (beside the document the 1440 frame above already shows it).
			if (width === 393) {
				await page.evaluate(async (id) => {
					const { setActiveSurface } = await import('/src/lib/stores/artifactWorkspace.ts');
					setActiveSurface(id, 'chat');
				}, threadId);
				// The opened cards have acknowledged the first two initial replies. A later
				// reply joins the remaining unread thread, making the compact two-item state real.
				await page.waitForTimeout(1100);
				addAgentReply(scenario.artifactId, scenario.roots[0], threadId, LATER_REPLIES[0]);
				await reloadComments(page, scenario.artifactId);
				await page.evaluate(async () => (await import('/src/lib/stores/attentionDock.svelte.ts')).reload());
				await resetDockPosition(page);
				await expect(page.locator('[data-attention-dock]')).toHaveCount(0);
				await expect(page.locator('.reply-chip-notice')).toContainText('2 new replies · launch-plan.md');
				await shot(page, `05b-reply-chip-next-reply-${theme}-${width}`);
			}

			// The reader leaves: a reply that arrives while the thread is open in view counts as seen.
			await page.goto('about:blank');
			// A reply is new when it is stamped later than the thread's seen stamp; wait it out so
			// this reply is deterministically newer than the one just viewed.
			await page.waitForTimeout(1100);
			LATER_REPLIES.forEach((body, index) => addAgentReply(scenario.artifactId, scenario.roots[index], threadId, body));
			await loadPastFirstRunLanding(page, () => page.goto(`/?token=${encodeURIComponent(TEST_SESSION_TOKEN)}`), { lands: true });
			await openThread(page, THREAD_TITLE);
			// The restored document tab may still be claiming the phone canvas while the thread settles;
			// turn to Chat until the thread's chat is the surface on screen.
			await expect(async () => {
				await page.evaluate(async (id) => {
					const { setActiveSurface } = await import('/src/lib/stores/artifactWorkspace.ts');
					setActiveSurface(id, 'chat');
				}, threadId);
				await expect(page.locator('.reply-chip-notice')).toBeVisible({ timeout: 1_000 });
			}).toPass({ timeout: 15_000 });
			// The persistent chip aggregates this chat's replies and lands on the first unread root.
			const chip = page.locator('.reply-chip-notice').getByRole('button');
			await expect(chip).toHaveCount(1);
			// One initially unread reply remains at desktop; the phone path adds one more
			// reply before leaving the document. The final chip must aggregate those with
			// the three replies that arrived after the reader left.
			await expect(chip).toContainText(`${width === 393 ? 5 : 4} new replies · launch-plan.md`);
			await shot(page, `06-reply-chip-${theme}-${width}`);
			await chip.click();
			await documentReady(page);
			await expect(page.locator(`[data-comment-id="${scenario.roots[0]}"]`)).toHaveClass(/focused/);
			await expect(page.locator(`[data-comment-id="${scenario.roots[0]}"]`)).toContainText(LATER_REPLIES[0]);
		});
	}
}

for (const width of [1440, 393]) {
	test(`${TAG} before state has neither in-document New marker nor chat dock at ${width}px`, async ({ page, request }, testInfo) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		skipMismatchedProject(width, testInfo.project.name);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		const threadId = await createSession(request, THREAD_TITLE);
		await onlyThread(request, threadId);
		const scenario = await seedScenario(page, request, threadId, `before-${width}`, []);
		await serveReplyQueue(page, request, scenario, threadId);
		await stageDocument(page, threadId, scenario.docPath, scenario.name, width);
		await documentReady(page);
		await expect(page.locator('.ct-new')).toHaveCount(0);
		await expect(page.getByRole('button', { name: '3 open threads' })).toBeVisible();
		await shot(page, `00a-before-in-document-light-${width}`);
		await page.evaluate(async (id) => {
			const { closeArtifactTab } = await import('/src/lib/stores/artifactWorkspace.ts');
			closeArtifactTab(id, 'reply-waiting');
		}, threadId);
		await expect(page.getByText(CHAT[0].content, { exact: true })).toBeVisible();
		await expect(page.locator('[data-attention-dock]')).toHaveCount(0);
		await shot(page, `00b-before-chat-light-${width}`);
	});

	for (const theme of ['light', 'dark'] as const) {
		test(`${TAG} ${theme} ${width}px: long names stay legible in the reply chip and the reply it opens`, async ({ page, request }, testInfo) => {
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			const scenario = await seedScenario(page, request, threadId, `long-dock-${theme}-${width}`, [0, 1, 2], { name: LONG_DOCUMENT_NAME, replier: LONG_REPLIER });
			await serveReplyQueue(page, request, scenario, threadId);
			await loginByUrl(page);
			await openThread(page, THREAD_TITLE);
			// The collapsed notice is the one reply chip; the long document name stays inside the viewport above the composer.
			const notice = page.locator('.reply-chip-notice');
			const chip = notice.getByRole('button');
			await expect(chip).toContainText(`3 new replies · ${LONG_DOCUMENT_NAME}`);
			// The tooltip and the accessible name carry the whole name, whatever the visible text shows.
			await expect(chip).toHaveAttribute('title', `3 new replies · ${LONG_DOCUMENT_NAME}`);
			await expect(chip).toHaveAccessibleName(`3 new replies · ${LONG_DOCUMENT_NAME}, document`);
			await expect(page.locator('[data-attention-dock]')).toHaveCount(0);
			await expect.poll(() => stackProblems(page, { chip: notice, composer: chatComposer(page) })).toEqual([]);
			await shot(page, `08-long-reply-chip-${theme}-${width}`);
			// The expanded reply is now the document's own card: the long replier name is shown there, on the first unread thread.
			await chip.click();
			await documentReady(page);
			const card = page.locator(`[data-comment-id="${scenario.roots[0]}"]`);
			await expect(card).toHaveClass(/focused/);
			await expect(card.locator('.ct-reply .ct-msg-author').getByText(LONG_REPLIER, { exact: true })).toBeVisible();
			await shot(page, `09-long-reply-document-${theme}-${width}`);
		});
	}

	test(`${TAG} long document names remain in the persistent reply chip at ${width}px`, async ({ page, request }, testInfo) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		skipMismatchedProject(width, testInfo.project.name);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		const threadId = await createSession(request, THREAD_TITLE);
		await onlyThread(request, threadId);
		const scenario = await seedScenario(page, request, threadId, `long-chip-${width}`, [0, 1, 2], { name: LONG_DOCUMENT_NAME, replier: LONG_REPLIER });
		await serveReplyQueue(page, request, scenario, threadId);
		await loadPastFirstRunLanding(page, () => page.goto(`/?token=${encodeURIComponent(TEST_SESSION_TOKEN)}`), { lands: true });
		await openThread(page, THREAD_TITLE);
		const chip = page.locator('.reply-chip-notice').getByRole('button');
		await expect(chip).toContainText('3 new replies');
		await expect(chip).toContainText(LONG_DOCUMENT_NAME);
		await shot(page, `10-long-chip-light-${width}`);
	});

	test(`${TAG} keyboard focus stays visible from the reply chip into the reply it opens at ${width}px`, async ({ page, request }, testInfo) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		skipMismatchedProject(width, testInfo.project.name);
		await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
		await page.emulateMedia({ colorScheme: 'light' });
		const threadId = await createSession(request, THREAD_TITLE);
		await onlyThread(request, threadId);
		const scenario = await seedScenario(page, request, threadId, `focus-${width}`);
		await serveReplyQueue(page, request, scenario, threadId);
		await loginByUrl(page);
		await openThread(page, THREAD_TITLE);
		// Replies never become a dock stop: the keyboard reaches the chip, then goes straight on to the composer.
		await expect(page.locator('[data-attention-dock]')).toHaveCount(0);
		const chip = page.locator('.reply-chip-notice').getByRole('button');
		await tabTo(page, '.reply-chip-notice button');
		await expect(chip).toBeFocused();
		await expect(chip).toContainText('3 new replies · launch-plan.md');
		await expect(chip).toHaveCSS('outline-style', 'solid');
		await shot(page, `04b-reply-chip-focus-light-${width}`);
		await page.keyboard.press('Tab');
		await expect(chatComposer(page)).toBeFocused();
		await shot(page, `04c-composer-focus-after-chip-light-${width}`);
		await page.keyboard.press('Shift+Tab');
		await expect(chip).toBeFocused();
		// Enter opens the document on the first unread reply, the destination the old card's Open in document reached.
		await page.keyboard.press('Enter');
		await documentReady(page);
		await expect(page.locator(`[data-comment-id="${scenario.roots[0]}"]`)).toHaveClass(/focused/);
		await shot(page, `04d-document-focus-after-chip-light-${width}`);
	});

	for (const theme of ['light', 'dark'] as const) {
		test(`${TAG} ${theme} ${width}px: failed document open gives the same recovery`, async ({ page, request }, testInfo) => {
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			const scenario = await seedScenario(page, request, threadId, `failed-open-${theme}-${width}`);
			await serveReplyQueue(page, request, scenario, threadId);
			await page.route('**/api/files/content**', async (route) => {
				if (route.request().method() !== 'GET') return route.continue();
				return route.abort('failed');
			});
			await loginByUrl(page);
			await openThread(page, THREAD_TITLE);
			await expect(page.locator('[data-attention-dock]')).toHaveCount(0);
			await page.locator('.reply-chip-notice').getByRole('button').click();
			const failurePanel = page.locator('.load-error');
			await expect(failurePanel).toBeVisible();
			await expect(failurePanel).toContainText("Couldn't open launch-plan.md.");
			await expect(failurePanel.getByRole('button', { name: 'Try again' })).toBeVisible();
			await shot(page, `07-failed-open-${theme}-${width}`);
			await failurePanel.getByRole('button', { name: 'Try again' }).click();
			await expect(failurePanel).toContainText("Couldn't open launch-plan.md.");
		});
	}
}

test(`${TAG} light 393px: same reply count keeps the responsive document UI unchanged`, async ({ page, request }, testInfo) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	skipMismatchedProject(393, testInfo.project.name);
	await page.setViewportSize({ width: 393, height: 852 });
	await page.emulateMedia({ colorScheme: 'light' });
	const threadId = await createSession(request, THREAD_TITLE);
	await onlyThread(request, threadId);
	// The main responsive journey keeps one shorter reader context so its one-unread,
	// offscreen checkpoint remains stable. This separate fixture proves the same UI
	// with the desktop thread's two reader follow-ups rather than changing that journey.
	const scenario = await seedScenario(page, request, threadId, 'same-count-393', [0, 1, 2], {
		readerFollowups: [
			{ rootIndex: 0, body: 'I checked the owner list against the release calendar and noted the two dependencies that must be confirmed before the launch date can hold. The rest of the work is already assigned, so this is only a request for confirmation rather than a new scope decision.' },
			{ rootIndex: 0, body: 'Noted.' },
		],
	});
	await serveReplyQueue(page, request, scenario, threadId);
	await loginByUrl(page);
	await openThread(page, THREAD_TITLE);
	await expect(page.locator('[data-attention-dock]')).toHaveCount(0);
	await page.locator('.reply-chip-notice').getByRole('button').click();
	await documentReady(page);
	await expect(page.locator(`[data-comment-id="${scenario.roots[0]}"]`)).toHaveClass(/focused/);
	await shot(page, '05c-same-count-light-393');
});

for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		test(`${TAG} ${theme} ${width}px: a removed document closes its reply hand-off without retry`, async ({ page, request }, testInfo) => {
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			const scenario = await seedScenario(page, request, threadId, `removed-${theme}-${width}`);
			let documentAvailable = true;
			await serveReplyQueue(page, request, scenario, threadId, () => documentAvailable);
			await loginByUrl(page);
			await openThread(page, THREAD_TITLE);
			const chip = page.locator('.reply-chip-notice').getByRole('button');
			await expect(chip).toContainText('3 new replies · launch-plan.md');
			await expect(page.locator('[data-attention-dock]')).toHaveCount(0);
			// The terminal state is an actual removal. Deleting the disposable file and
			// reconciling its registered row keeps the pane and document surface truthful.
			rmSync(scenario.docPath);
			const reconciled = await request.post(`/api/threads/${threadId}/artifacts/reconcile?token=${encodeURIComponent(TEST_TOKEN)}`);
			expect(reconciled.ok(), await reconciled.text()).toBeTruthy();
			expect((await reconciled.json()).counts.applied).toBe(1);
			// The real reconciler emits this frame after it commits. This request-context
			// fixture has no browser WebSocket transport, so deliver the exact event to
			// the mounted pane and prove its normal refetch removes the stale rail row.
			await page.evaluate(async (id) => {
				const { emit } = await import('/src/lib/ws/events.ts');
				emit('artifacts_changed', { affected_thread_ids: [id] });
			}, threadId);
			documentAvailable = false;
			await chip.click();
			const removed = page.locator('.gone');
			await expect(removed.getByText('launch-plan.md was removed.', { exact: true })).toBeVisible();
			await expect(removed.getByRole('button', { name: 'Try again' })).toHaveCount(0);
			await expect(page.locator('.row.openable', { hasText: 'launch-plan.md' })).toHaveCount(0);
			await shot(page, `07b-removed-document-${theme}-${width}`);
			await removed.getByRole('button', { name: 'Close tab' }).click();
			await expect(page.getByText(CHAT[0].content, { exact: true })).toBeVisible();
			// The removed document's reply hand-off is gone from Chat, and nothing moved into Needs you.
			await expect(page.locator('.reply-chip-notice')).toHaveCount(0);
			await expect(page.locator('[data-attention-dock]')).toHaveCount(0);
		});
	}
}

test(`${TAG} real queue: open the chip and read its reply`, async ({ page, request }, testInfo) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	skipMismatchedProject(1440, testInfo.project.name);
	await page.setViewportSize({ width: 1440, height: 900 });
	const threadId = await createSession(request, THREAD_TITLE);
	await onlyThread(request, threadId);
	const scenario = await seedScenario(page, request, threadId, 'real-reply-queue', [0]);
	const queue = await request.get('/api/attention/queue', { params: { token: TEST_TOKEN } });
	expect(queue.ok()).toBeTruthy();
	expect((await queue.json()).reply_items[0].detail.unread_count).toBe(1);
	await loginByUrl(page);
	await openThread(page, THREAD_TITLE);
	const notice = page.locator('.reply-chip-notice');
	await expect(notice.getByRole('button')).toContainText('1 new reply · launch-plan.md');
	await expect(notice.locator('.ct-new-dot')).toBeVisible();
	await notice.getByRole('button').click();
	await documentReady(page);
	await expect(page.getByText(PASSAGES[0].reply, { exact: true })).toBeVisible();
	await expect.poll(async () => {
		const response = await request.get('/api/attention/queue', { params: { token: TEST_TOKEN } });
		return ((await response.json()).reply_items ?? []).length;
	}, { timeout: 10_000 }).toBe(0);
	await expect(notice).toHaveCount(0);
	expect(scenario.roots).toHaveLength(3);
});

// A reply counts as read once the document has shown it whole for the 1.5 s dwell (CommentThread.svelte, VIEWED_DWELL_MS).
// The server then drops it from the thread's unread replies and the chip's count follows the queue.
for (const theme of ['light', 'dark'] as const) {
	for (const width of [1440, 393]) {
		test(`${TAG} ${theme} ${width}px: reading replies shrinks the chip and reading them all clears it`, async ({ page, request }, testInfo) => {
			test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
			skipMismatchedProject(width, testInfo.project.name);
			await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
			await page.emulateMedia({ colorScheme: theme });
			const threadId = await createSession(request, THREAD_TITLE);
			await onlyThread(request, threadId);
			const scenario = await seedScenario(page, request, threadId, `read-${theme}-${width}`, [0, 1, 2], { readerFollowups: partialReadFollowups(width) });
			await loginByUrl(page);
			await openThread(page, THREAD_TITLE);
			const notice = page.locator('.reply-chip-notice');
			await expect(notice.getByRole('button')).toContainText('3 new replies · launch-plan.md');
			await notice.getByRole('button').click();
			await documentReady(page);
			// The first two replies are on screen for the dwell; the third is still below the sheet.
			await page.waitForTimeout(1_700);
			await expect(page.getByRole('button', { name: '3 open threads, 1 unread', exact: true })).toBeVisible();
			const showSurface = (surface: 'chat' | 'artifact') => width === 393
				? page.evaluate(async ({ id, next }) => {
					const { setActiveSurface } = await import('/src/lib/stores/artifactWorkspace.ts');
					setActiveSurface(id, next);
				}, { id: threadId, next: surface })
				: Promise.resolve();
			const reloadQueue = async () => {
				// Re-read the server projection after the viewed stamp.
				await page.evaluate(async () => (await import('/src/lib/stores/attentionDock.svelte.ts')).reload());
			};
			await showSurface('chat');
			await reloadQueue();
			await expect(notice.getByRole('button')).toContainText('1 new reply · launch-plan.md');
			await expect(page.locator('[data-attention-dock]')).toHaveCount(0);
			await shot(page, `11a-reply-chip-partly-read-${theme}-${width}`);

			// The reader scrolls to the last unread reply and it stays on screen for the dwell.
			await showSurface('artifact');
			// On the phone the comments sheet closed with the surface; the document's own pill opens it again.
			if (width === 393) await page.getByRole('button', { name: '3 open threads, 1 unread', exact: true }).click();
			await page.locator(`[data-comment-id="${scenario.roots[2]}"]`).evaluate((card) => card.scrollIntoView({ block: 'center' }));
			await page.waitForTimeout(1_700);
			await expect(page.locator('.ct-new')).toHaveCount(0);
			await showSurface('chat');
			await reloadQueue();
			await expect(notice).toHaveCount(0);
			await expect(page.locator('[data-attention-dock]')).toHaveCount(0);
			await expect(chatComposer(page)).toBeVisible();
			await shot(page, `11b-reply-chip-all-read-${theme}-${width}`);
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
				'03a-reply-chip-needs-you-stack',
				'03b-reply-chip-two-documents',
				'04-reply-chip',
				'05-open-in-document',
				'06-reply-chip',
				'07-failed-open',
				'07b-removed-document',
				'08-long-reply-chip',
				'09-long-reply-document',
				'11a-reply-chip-partly-read',
				'11b-reply-chip-all-read',
			]) expected.add(`${frame}-${theme}-${width}.png`);
			if (width === 1440) {
				expected.add(`01-document-arrival-${theme}-${width}-rail-closed.png`);
				expected.add(`03-three-replies-${theme}-${width}-rail-closed.png`);
				expected.add(`05-open-in-document-${theme}-${width}-rail-closed.png`);
			}
			if (width === 393) expected.add(`05b-reply-chip-next-reply-${theme}-${width}.png`);
		}
	}
	for (const width of [1440, 393]) {
		for (const frame of [
			'00a-before-in-document',
			'00b-before-chat',
			'04b-reply-chip-focus',
			'04c-composer-focus-after-chip',
			'04d-document-focus-after-chip',
			'10-long-chip',
		]) expected.add(`${frame}-light-${width}.png`);
	}
	expected.add('05c-same-count-light-393.png');

	const runStartedAt = statSync(path.join(screenshotDir, EVIDENCE_RUN_MARKER)).mtimeMs;
	const capturedFrames = readdirSync(screenshotDir).filter((entry) => entry.endsWith('.png')).sort();
	expect(capturedFrames, 'The empty evidence directory contains exactly the current run’s 81 frames').toEqual([...expected].sort());
	for (const frame of expected) {
		const output = path.join(screenshotDir, frame);
		expect(existsSync(output), `Persisted reply-awareness evidence frame ${output}`).toBeTruthy();
		expect(statSync(output).mtimeMs, `Evidence frame ${output} was written after this run started`).toBeGreaterThanOrEqual(runStartedAt);
	}
});

export { TAG };
