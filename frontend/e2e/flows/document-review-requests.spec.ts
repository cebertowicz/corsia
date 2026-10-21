import { execFileSync } from 'node:child_process';
import { writeFile } from 'node:fs/promises';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';
import { expectContrast, TEXT } from '../support/contrast';
import { openUnsentFooter, visibleHeaderSends } from '../support/docFeedbackFooter';
import { demoteAgent } from '../support/sharedLiveDocument';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const auth = () => ({ Authorization: `Bearer ${TEST_TOKEN}` });
type SendState = 'pending' | 'grace' | 'undelivered';

/** Every reviewer a case promoted; the agent rail lists a few before "Show more", so each case demotes its own and later specs still find their agents. */
const promoted: string[] = [];

test.afterEach(async ({ request }) => {
	for (const id of promoted.splice(0)) await demoteAgent(request, id);
});

async function reviewers(request: APIRequestContext, stamp: string) {
	return Promise.all(Array.from({ length: 10 }, async (_, index) => {
		const name = `Review Agent ${stamp} ${index}`;
		const id = await createSession(request, name);
		const response = await request.patch(`/api/agents/${id}`, { headers: auth(), data: { responsibility: 'Reviews this document.', active: true } });
		expect(response.ok(), await response.text()).toBeTruthy();
		promoted.push(id);
		return { id, name };
	}));
}
async function seedDocument(request: APIRequestContext, title: string) {
	const threadId = await createSession(request, title);
	const roots = await request.get('/api/files/tree', { params: { token: TEST_TOKEN } });
	expect(roots.ok(), await roots.text()).toBeTruthy();
	const workspace = (await roots.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path;
	const name = `${title}.md`, docPath = path.join(workspace, name);
	const text = '# Review document\n\n' + Array.from({ length: 40 }, (_, index) => `Paragraph ${index + 1}: enough document context to read.\n\n`).join('');
	const seeded = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: docPath, text, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	return { threadId, title, name, docPath, artifactId: (await seeded.json()).artifact.id as string };
}
type DocumentFixture = Awaited<ReturnType<typeof seedDocument>>;
async function openDocument(page: Page, doc: DocumentFixture, splitWidth = 500) {
	await page.addInitScript(({ doc, splitWidth }) => {
		localStorage.setItem('corsia_theme', 'auto');
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [doc.threadId]: {
			tabs: [{ instanceId: doc.threadId, artifactId: doc.artifactId, path: doc.docPath, title: doc.name, loadedRevision: null, latestRevision: null, status: 'loading' }],
			activeTabId: doc.threadId, visible: true, activeSurface: 'artifact',
		} }, splitWidth }));
	}, { doc, splitWidth });
	await loginByUrl(page, { waitForComposer: false });
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').filter({ hasText: doc.title }).click();
	await expect(page.locator('.cm-content')).toContainText('Paragraph 1:');
	await expect(page.locator('.document-tools').getByRole('button', { name: 'Request review', exact: true })).toBeVisible();
}
async function requestReview(request: APIRequestContext, doc: DocumentFixture, reviewer: string) {
	const response = await request.post('/api/doc-reviews', { headers: auth(), data: {
		artifact_id: doc.artifactId, thread_id: doc.threadId, reviewer, client_id: crypto.randomUUID(),
	} });
	expect(response.ok(), await response.text()).toBeTruthy();
}

function rewriteGeneratedBuildInfo() {
	// Exercise the exact metadata producer which previously invalidated Vite's
	// version-consumer boundary. This invokes one fixed project script, never a
	// caller-supplied shell command or workspace path.
	execFileSync(process.execPath, [path.join(repoRoot, 'frontend', 'scripts', 'generate-build-info.mjs')], {
		cwd: path.join(repoRoot, 'frontend'), encoding: 'utf8',
	});
}

/** A drawer closes with its Close button; the desktop popover closes with Escape. */
function closeReviewControls(page: Page, narrow: boolean) {
	return { click: () => narrow ? page.getByRole('button', { name: 'Close', exact: true }).click() : page.keyboard.press('Escape'),
		button: page.getByRole('button', { name: 'Close', exact: true }) };
}

async function reviewSurface(page: Page) {
	const contentWidth = await page.locator('.doc').evaluate((element) => {
		const style = getComputedStyle(element);
		return element.clientWidth - Number.parseFloat(style.paddingLeft) - Number.parseFloat(style.paddingRight);
	});
	const narrow = contentWidth <= 600;
	return {
		contentWidth,
		narrow,
		panel: page.locator(narrow ? '[data-adaptive-drawer]' : '.popover'),
		close: closeReviewControls(page, narrow),
	};
}

// Layout fixtures affect only the disposable CI mock database. Review creation,
// cancellation and settlement below always cross the real API/service boundary.
function setFeedbackFixture(artifactId: string, state: SendState) {
	const root = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !root) throw new Error('Requires disposable E2E mock state');
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), ['-c', [
		'import sqlite3, sys',
		'from datetime import datetime, timedelta, timezone',
		'connection = sqlite3.connect(sys.argv[1])',
		'artifact, state = sys.argv[2:]',
		'future = (datetime.now(timezone.utc) + timedelta(minutes=10)).isoformat()',
		'changed = connection.execute("UPDATE doc_feedback_batches SET state=?, closed_reason=?, closes_at=?, undelivered_reason=? WHERE artifact_id=? AND author_kind=\'user\'", ("undelivered" if state == "undelivered" else "open", "leave" if state == "grace" else None, future, "owner_admission_refused" if state == "undelivered" else None, artifact)).rowcount',
		'assert changed == 1, changed',
		'changed = connection.execute("UPDATE doc_comments SET anchor_text=?, from_off=NULL, to_off=NULL, anchor_prefix=NULL, anchor_suffix=NULL WHERE artifact_id=?", ("A quote removed from this document.", artifact)).rowcount',
		'assert changed == 1, changed',
		'connection.commit()',
		'connection.close()',
	].join('\n'), path.join(root, 'corsia.db'), artifactId, state], { cwd: repoRoot, encoding: 'utf8' });
}
async function captureLayout(page: Page, info: TestInfo, label: string) {
	const measured = await page.locator('.doc').evaluate((root) => {
		const header = root.querySelector('.document-header')!.getBoundingClientRect();
		const editor = root.querySelector('.doc-main')!.getBoundingClientRect();
		const canvas = root.getBoundingClientRect();
		const row = root.querySelector('.document-status-row')!;
		const tools = root.querySelector('.document-tools')!;
		const retry = row.querySelector<HTMLButtonElement>('.undelivered > .btn');
		const retryInset = retry ? getComputedStyle(retry, '::after').inset : null;
		const geometry = (element: Element) => {
			const rect = element.getBoundingClientRect(), style = getComputedStyle(element);
			return {
				tag: element.tagName, classes: element.className, text: element.textContent?.trim().slice(0, 160),
				left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, width: rect.width, height: rect.height,
				clientWidth: element.clientWidth, scrollWidth: element.scrollWidth,
				flex: style.flex, flexBasis: style.flexBasis, flexWrap: style.flexWrap, minWidth: style.minWidth,
				maxWidth: style.maxWidth, overflowX: style.overflowX, whiteSpace: style.whiteSpace,
				outsideCanvas: Math.max(0, rect.right - canvas.right, canvas.left - rect.left),
			};
		};
		return {
			canvasWidth: canvas.width, headerHeight: header.height, readingAreaHeight: editor.height,
			viewportWidth: window.innerWidth,
			overflow: root.scrollWidth - root.clientWidth,
			root: geometry(root), header: geometry(root.querySelector('.document-header')!),
			row: geometry(row), retryInset, descendants: [...row.querySelectorAll('*')].map(geometry),
			headerDescendants: [...root.querySelector('.document-header')!.querySelectorAll('*')].map(geometry),
			controls: [...tools.querySelectorAll('button'), ...row.querySelectorAll('button')].map((button) => {
				const rect = button.getBoundingClientRect();
				return { name: button.textContent?.trim(), left: rect.left, right: rect.right, top: rect.top, bottom: rect.bottom, height: rect.height };
			}),
		};
	});
	const measurementPath = info.outputPath(`${label}-measurements.json`);
	await writeFile(measurementPath, JSON.stringify(measured, null, 2));
	await info.attach(`${label}-measurements`, { path: measurementPath, contentType: 'application/json' });
	console.info(`Review layout ${label}: ${JSON.stringify(measured)}`);
	await saveEvidenceScreenshot(page, info, label, { fullPage: false });
	expect(measured.canvasWidth).toBeLessThanOrEqual(600);
	expect(measured.overflow).toBeLessThanOrEqual(1);
	expect(measured.row.scrollWidth).toBe(measured.row.clientWidth);
	if (measured.retryInset !== null) expect(measured.retryInset).toBe('0px');
	expect(measured.readingAreaHeight).toBeGreaterThan(0);
	for (const control of measured.controls) expect(control.height, control.name).toBeGreaterThanOrEqual(44);
	for (const [index, a] of measured.controls.entries()) for (const b of measured.controls.slice(index + 1)) {
		const overlap = Math.min(a.right, b.right) - Math.max(a.left, b.left) > 1 && Math.min(a.bottom, b.bottom) - Math.max(a.top, b.top) > 1;
		expect(overlap, `${a.name} overlaps ${b.name}`).toBe(false);
	}
}

const composedFixtures = [
	{ viewport: 'desktop-1280', width: 1280, height: 900, splitWidth: 820, expectedNarrow: true },
	{ viewport: 'desktop-1440', width: 1440, height: 1000, splitWidth: 820, expectedNarrow: true },
	{ viewport: 'phone-393', width: 393, height: 852, splitWidth: 500, expectedNarrow: true },
	{ viewport: 'wide-desktop-1920', width: 1920, height: 1000, splitWidth: 820, expectedNarrow: false },
] as const;

for (const fixture of composedFixtures) for (const theme of ['light', 'dark'] as const) {
	const { viewport, width, height, splitWidth, expectedNarrow } = fixture;
	test(`@flow:document-review-requests composed ${viewport} ${theme}`, async ({ page, request }, info) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable synthetic state');
		test.setTimeout(100_000);
		const stamp = `${viewport}-${theme}-${Date.now()}`;
		await page.setViewportSize({ width, height });
		await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
		const doc = await seedDocument(request, `review-composed-${stamp}`);
		const agents = await reviewers(request, stamp);
		await openDocument(page, doc, splitWidth);
		await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
		const trigger = page.locator('.document-tools').getByRole('button', { name: 'Request review', exact: true });
		await trigger.click();
		const surface = await reviewSurface(page);
		expect(surface.narrow, `${viewport} document content width ${surface.contentWidth}`).toBe(expectedNarrow);
		await expect(surface.panel).toBeVisible();
		const search = page.getByRole('combobox', { name: 'Add reviewers' });
		await expect(search).toBeFocused();
		// With all ten reviewers listed, each row leads with the agent's name and the footer stays inside the panel.
		const firstOption = surface.panel.getByRole('option').first();
		await expect(firstOption.locator('.option-name')).toHaveText(/^Review Agent /);
		const fit = await surface.panel.evaluate((panel) => {
			const box = panel.getBoundingClientRect(), footer = panel.querySelector('footer')?.getBoundingClientRect();
			return footer && { inside: footer.top >= box.top && footer.bottom <= box.bottom + 0.5, onScreen: footer.bottom <= window.innerHeight + 0.5 };
		});
		expect(fit, `${viewport} picker footer`).toEqual({ inside: true, onScreen: true });
		// The name reads first: larger and in a different colour than the responsibility line under it.
		const rank = await firstOption.evaluate((row) => {
			const name = getComputedStyle(row.querySelector('.option-name')!), duty = getComputedStyle(row.querySelector('.option-detail')!);
			return { larger: parseFloat(name.fontSize) > parseFloat(duty.fontSize), distinctColour: name.color !== duty.color };
		});
		expect(rank, `${viewport} reviewer name hierarchy`).toEqual({ larger: true, distinctColour: true });
		await saveEvidenceScreenshot(page, info, `review-composed-${viewport}-${theme}-open`, { fullPage: false });
		await search.fill(agents[0].name); await search.press('Home'); await search.press('Enter');
		const remove = page.getByRole('button', { name: `Remove ${agents[0].name}`, exact: true });
		await expect(remove).toBeVisible(); await expect(search).toBeFocused(); await remove.click(); await expect(remove).toHaveCount(0); await expect(search).toBeFocused();
		await search.press('Home'); await search.press('Enter');
		if (surface.narrow) expect((await surface.close.button.boundingBox())?.height).toBeGreaterThanOrEqual(44);
		await saveEvidenceScreenshot(page, info, `review-composed-${viewport}-${theme}-selected`, { fullPage: false });
		await surface.panel.getByRole('button', { name: 'Send', exact: true }).click();
		// Sending closes the surface, confirms with a toast and leaves the new reviewer in the header.
		await expect(page.getByText(/^Review requested from /)).toBeVisible();
		await expect(page.locator('.reviewer-group')).toBeVisible();
		await expect(trigger).toBeFocused();
		await saveEvidenceScreenshot(page, info, `review-composed-${viewport}-${theme}-sent`, { fullPage: false });
		await page.locator('.reviewer-group').click(); await expect(page.locator('[data-review-request]')).toHaveCount(1);
		await saveEvidenceScreenshot(page, info, `review-composed-${viewport}-${theme}-list`, { fullPage: false });
		// Closing the list returns focus to the control that opened it.
		await surface.close.click(); await expect(page.locator('.reviewer-group')).toBeFocused();
		await trigger.click(); await expect(search).toBeFocused(); await search.press('Escape'); await expect(trigger).toBeFocused();
	});
}

test('@flow:document-review-requests generated metadata does not interrupt an open picker', async ({ page, request }) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable synthetic state');
	test.setTimeout(30_000);
	await page.setViewportSize({ width: 1280, height: 900 });
	await page.emulateMedia({ reducedMotion: 'reduce' });
	const stamp = `metadata-${Date.now()}`;
	const doc = await seedDocument(request, `review-${stamp}`);
	const agents = await reviewers(request, stamp);
	await openDocument(page, doc, 820);
	const trigger = page.locator('.document-tools').getByRole('button', { name: 'Request review', exact: true });
	await trigger.click();
	const search = page.getByRole('combobox', { name: 'Add reviewers' });
	await search.fill(agents[0].name); await search.press('Home'); await search.press('Enter');
	const remove = page.getByRole('button', { name: `Remove ${agents[0].name}`, exact: true });
	await expect(remove).toBeVisible();
	let navigations = 0, requests = 0;
	const navigated = (frame: import('@playwright/test').Frame) => { if (frame === page.mainFrame()) navigations += 1; };
	const requested = (outgoing: import('@playwright/test').Request) => { if (new URL(outgoing.url()).pathname === '/api/doc-reviews' && outgoing.method() === 'POST') requests += 1; };
	page.on('framenavigated', navigated); page.on('request', requested);
	rewriteGeneratedBuildInfo();
	// The captured failure's watcher delivery arrived just over seven seconds after
	// the generator write. Keep the picker open past that window before submitting.
	await page.waitForTimeout(8_000);
	expect(navigations).toBe(0); await expect(remove).toBeVisible();
	await page.getByRole('button', { name: 'Send', exact: true }).click();
	await expect(page.getByText(/^Review requested from /)).toBeVisible(); await expect(page.locator('.reviewer-group')).toBeVisible();
	expect(requests).toBe(1);
	page.off('framenavigated', navigated); page.off('request', requested);
});

test('@flow:document-review-requests request, list, cancel and settlement without reload', async ({ page, request }, info) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable synthetic state');
	test.setTimeout(100_000);
	const mobile = info.project.name.includes('mobile');
	await page.setViewportSize(mobile ? { width: 393, height: 852 } : { width: 1440, height: 1000 });
	await page.emulateMedia({ reducedMotion: 'reduce' });
	const stamp = String(Date.now()), agents = await reviewers(request, stamp);
	const doc = await seedDocument(request, `review-journey-${stamp}`);
	const frames: { type?: string; artifact_id?: string }[] = [];
	page.on('websocket', (socket) => socket.on('framereceived', (frame) => {
		try { frames.push(JSON.parse(String(frame.payload))); } catch { /* Only inspect JSON frames. */ }
	}));
	// Exercise summary → cancel where the Workspace pane overlays a compact artifact canvas.
	if (!mobile) await page.setViewportSize({ width: 1280, height: 1000 });
	await openDocument(page, doc, mobile ? 500 : 820);
	const trigger = page.locator('.document-tools').getByRole('button', { name: 'Request review', exact: true });
	await trigger.click();
	const surface = await reviewSurface(page);
	await expect(surface.panel).toBeVisible();
	const search = page.getByRole('combobox', { name: 'Add reviewers' });
	await expect(search).toBeFocused();
	await search.fill(agents[0].name); await search.press('Home'); await search.press('Enter');
	await page.getByRole('button', { name: 'Send', exact: true }).click();
	const list = page.locator('[data-review-surface]');
	const summary = page.locator('.reviewer-group'); await expect(summary).toBeVisible();
	await expect(page.getByText(/^Review requested from /)).toBeVisible(); await expect(trigger).toBeFocused();
	await requestReview(request, doc, agents[1].id);
	await expect(summary.locator('.reviewer')).toHaveCount(2);
	await summary.click(); await expect(list.locator('[data-review-request]')).toHaveCount(2);
	await expect(list).toContainText(agents[0].name); await expect(list.locator('[data-review-request] .status').first()).toHaveText(/Requested \d/);
	const cancel = list.getByRole('button', { name: /^Cancel request to / });
	await cancel.first().click(); await expect(list.locator('[data-review-request]')).toHaveCount(1);
	await cancel.click(); await expect(list.locator('[data-review-request]')).toHaveCount(0);
	await surface.close.click(); await expect(trigger).toBeFocused();
	await requestReview(request, doc, agents[0].id);
	await expect(summary).toBeVisible(); await summary.click();
	frames.length = 0;
	// Counters rule out document reloads and navigations during settlement.
	let reads = 0, navigations = 0;
	page.on('request', (outgoing) => { if (new URL(outgoing.url()).pathname === '/api/files/content') reads += 1; });
	page.on('framenavigated', (frame) => { if (frame === page.mainFrame()) navigations += 1; });
	const reviewed = await request.post('/api/delegation/docs/comments', { params: { token: TEST_TOKEN }, data: {
		artifact_id: doc.artifactId, as_session_id: agents[0].id, review: true, body: 'No findings.', submit: true,
	} });
	expect(reviewed.ok(), await reviewed.text()).toBeTruthy();
	await expect.poll(() => frames.some((frame) => frame.type === 'artifacts_changed' && frame.artifact_id === doc.artifactId), { timeout: 10_000 }).toBe(true);
	await expect(list.locator('[data-review-request]')).toHaveCount(0);
	expect(frames.some((frame) => frame.type === 'artifacts_changed' && frame.artifact_id === doc.artifactId)).toBe(true);
	expect(reads).toBe(0); expect(navigations).toBe(0);
	await surface.close.click(); await expect(trigger).toBeFocused();
	await saveEvidenceScreenshot(page, info, 'review-settled-without-reload', { fullPage: false });
});

test('@flow:document-review-requests closed continuation refreshes on Plans change without reload', async ({ page, request }, info) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable synthetic state');
	test.setTimeout(100_000);
	const mobile = info.project.name.includes('mobile');
	await page.setViewportSize(mobile ? { width: 393, height: 852 } : { width: 1920, height: 1000 });
	await page.emulateMedia({ reducedMotion: 'reduce' });
	const stamp = String(Date.now()), agents = await reviewers(request, stamp);
	const doc = await seedDocument(request, `review-silent-${stamp}`);
	await requestReview(request, doc, agents[0].id);
	const listed = await request.get(`/api/artifacts/${doc.artifactId}/reviews`, { headers: auth() });
	expect(listed.ok(), await listed.text()).toBeTruthy();
	let row = (await listed.json()).reviews[0];
	expect(row).toMatchObject({ state: 'forked', node_id: null });
	// Reuse this suite's disposable SQLite fixture route to exercise the real continuation creator.
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !stateRoot) throw new Error('Requires disposable E2E mock state');
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), ['-c', [
		'import os, sqlite3, sys',
		"sys.path.insert(0, os.path.join(os.getcwd(), 'backend'))",
		'from doceditor.review_requests import escalate_fork',
		'connection = sqlite3.connect(sys.argv[1])',
		'connection.row_factory = sqlite3.Row',
		"changed = connection.execute(\"UPDATE doc_review_requests SET state='forked', node_id=NULL, fork_id='e2e-fixture', answered_batch_id=NULL, reason=NULL WHERE id=?\", (sys.argv[2],)).rowcount",
		'assert changed == 1, changed',
		'connection.commit()',
		'request = escalate_fork(connection, sys.argv[2])',
		"assert request and request['state'] == 'handed_back' and request['node_id'], request",
	].join('\n'), path.join(stateRoot, 'corsia.db'), row.id], {
		cwd: repoRoot,
		encoding: 'utf8',
		env: { ...process.env, CORSIA_WORKSPACE: process.env.CORSIA_E2E_WORKSPACE, CORSIA_STATE_DIR: stateRoot, CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1' },
	});
	const handedBack = await request.get(`/api/artifacts/${doc.artifactId}/reviews`, { headers: auth() });
	expect(handedBack.ok(), await handedBack.text()).toBeTruthy();
	row = (await handedBack.json()).reviews[0];
	expect(row).toMatchObject({ state: 'handed_back' });
	expect(row.node_id).toEqual(expect.any(String));
	const frames: { type?: string; artifact_id?: string }[] = [];
	page.on('websocket', (socket) => socket.on('framereceived', (frame) => {
		try { frames.push(JSON.parse(String(frame.payload))); } catch { /* Only inspect JSON frames. */ }
	}));
	await openDocument(page, doc, mobile ? 500 : 820);
	await page.locator('.reviewer-group').click();
	const list = page.locator('[data-review-surface]');
	await expect(list.locator('[data-review-request]')).toHaveCount(1);
	await expect(list.getByRole('button', { name: /^Cancel request to / })).toBeVisible();
	frames.length = 0;
	let reads = 0, navigations = 0;
	page.on('request', (outgoing) => { if (new URL(outgoing.url()).pathname === '/api/files/content') reads += 1; });
	page.on('framenavigated', (frame) => { if (frame === page.mainFrame()) navigations += 1; });
	const operator = await request.get(`/api/plans/operator/threads/${agents[0].id}`, { headers: auth() });
	expect(operator.ok(), await operator.text()).toBeTruthy();
	const plan = (await operator.json()).plans.find((item: { nodes: { id: string }[] }) => item.nodes.some((node) => node.id === row.node_id));
	expect(plan).toBeTruthy();
	const node = plan.nodes.find((item: { id: string }) => item.id === row.node_id);
	const closed = await request.post(`/api/plans/${plan.plan.id}/mutations`, { headers: auth(), data: {
		operation: 'close_node', mutation_id: crypto.randomUUID(), params: {
			node_id: node.id, expected_revision: node.revision, reason: 'Review continuation completed',
		},
	} });
	expect(closed.ok(), await closed.text()).toBeTruthy();
	await expect.poll(() => frames.some((frame) => frame.type === 'plans_changed'), { timeout: 10_000 }).toBe(true);
	await expect(list.locator('[data-review-request]')).toHaveCount(0);
	await expect(page.locator('.reviewer-group')).toHaveCount(0);
	expect(frames.some((frame) => frame.type === 'artifacts_changed' && frame.artifact_id === doc.artifactId)).toBe(false);
	expect(reads).toBe(0); expect(navigations).toBe(0);
	await saveEvidenceScreenshot(page, info, 'review-handed-back-closed-without-reload', { fullPage: false });
	const late = await request.post('/api/delegation/docs/comments', { params: { token: TEST_TOKEN }, data: {
		artifact_id: doc.artifactId, as_session_id: agents[0].id, review: true, body: 'A later review comment.', submit: true,
	} });
	expect(late.ok(), await late.text()).toBeTruthy();
	await expect(list.locator('[data-review-request]')).toHaveCount(0);
	expect(reads).toBe(0); expect(navigations).toBe(0);
});

for (const count of [0, 1, 10]) for (const state of ['pending', 'grace', 'undelivered'] as const) {
	test(`@flow:document-review-requests ${count} reviews with ${state} send and changed text`, async ({ page, request }, info) => {
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable synthetic state');
		test.setTimeout(100_000);
		const mobile = info.project.name.includes('mobile');
		await page.setViewportSize(mobile ? { width: 393, height: 852 } : { width: 1440, height: 1000 });
		await page.emulateMedia({ reducedMotion: 'reduce' });
		const stamp = `${Date.now()}-${count}-${state}`;
		const doc = await seedDocument(request, `review-layout-${stamp}`);
		const agents = count ? await reviewers(request, stamp) : [];
		for (const agent of agents.slice(0, count)) await requestReview(request, doc, agent.id);
		const comment = await request.post(`/api/artifacts/${doc.artifactId}/comments`, { params: { token: TEST_TOKEN }, data: {
			body: 'Check this removed quote.', anchor_text: 'Review document', from_off: 2, to_off: 17,
		} });
		expect(comment.ok(), await comment.text()).toBeTruthy();
		setFeedbackFixture(doc.artifactId, state);
		await openDocument(page, doc);
		await captureLayout(page, info, `reviews-${count}-${state}-${mobile ? 'phone' : 'desktop-narrow'}`);
		await expect(page.locator('.comment-status:has(.unplaced-dot)')).toBeVisible();
		if (state !== 'undelivered') {
			// Send now and the countdown are in the comments drawer's footer; the header shows neither.
			expect(await visibleHeaderSends(page), 'no Send shows in the header').toEqual([]);
			const footer = await openUnsentFooter(page);
			await expect(footer.getByRole('status')).toContainText(state === 'grace' ? '1 message not sent yet · sending to ' : '1 message not sent yet · it goes to ');
			await expect(footer.getByRole('button', { name: 'Send now', exact: true })).toBeVisible();
			await page.getByRole('region', { name: 'Document comments' }).getByRole('button', { name: 'Close comments', exact: true }).click();
			// The closing drawer stays mounted (inert) through its outro; the review panel below must be the only drawer.
			await expect(page.locator('[data-adaptive-drawer]')).toHaveCount(0);
		}
		if (state === 'undelivered') await expect(page.locator('.undelivered').getByRole('button', { name: 'Retry', exact: true })).toBeVisible();
		await expect(page.locator('.reviewer-group')).toHaveCount(count ? 1 : 0);
		if (state === 'undelivered') await expectContrast(page, info, `reviews-${count}-retry-${mobile ? 'phone' : 'desktop-narrow'}`, {
			retry: { target: page.locator('.undelivered').getByRole('button', { name: 'Retry', exact: true }), min: TEXT },
		});
		if (count) {
			await page.locator('.reviewer-group').click();
			const surface = await reviewSurface(page);
			await expect(surface.panel).toBeVisible();
			await expect(page.locator('[data-review-request]')).toHaveCount(count);
			await saveEvidenceScreenshot(page, info, `reviews-${count}-${state}-list`, { fullPage: false });
		}
	});
}
