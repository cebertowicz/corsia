import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { syntheticPng } from '../support/syntheticImages';

/**
 * The real transcript route for a review ask. The fixture records an assistant tool call and then raises the
 * corresponding hold through the Plan service, so cards are attached by the production read model rather than a
 * browser stub. Clicking the row exercises the same artifact workspace open path as the pane.
 */
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const backendDir = path.join(repoRoot, 'backend');
const python = () => process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python');

type Artifact = {
	threadId: string;
	title: string;
	name: string;
	path: string;
	artifactId: string;
};

async function seedArtifact(request: APIRequestContext, baseTitle: string, image = false): Promise<Artifact> {
	const title = `${baseTitle} ${Math.random().toString(36).slice(2, 7)}`;
	const threadId = await createSession(request, title);
	const roots = await request.get('/api/files/tree', { params: { token: TEST_TOKEN } });
	const workspace = (await roots.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path;
	const name = `${title}.${image ? 'png' : 'md'}`;
	const artifactPath = path.join(workspace, name);
	if (image) {
		fs.writeFileSync(artifactPath, syntheticPng(640, 400));
		const registered = await request.post('/api/files/register', { params: { token: TEST_TOKEN }, data: { path: artifactPath, thread_id: threadId } });
		expect(registered.ok(), await registered.text()).toBeTruthy();
		return { threadId, title, name, path: artifactPath, artifactId: (await registered.json()).artifact.id as string };
	}
	const saved = await request.put('/api/files/content', {
		params: { token: TEST_TOKEN },
		data: { path: artifactPath, text: '# Rollout plan\n\nReady for review.\n', thread_id: threadId, create: true },
	});
	expect(saved.ok(), await saved.text()).toBeTruthy();
	return { threadId, title, name, path: artifactPath, artifactId: (await saved.json()).artifact.id as string };
}

/** Nine registered references on one message exercise the real card cap, desktop wrap, and phone scroll cue. */
async function seedChipArtifacts(request: APIRequestContext, baseTitle: string): Promise<Artifact> {
	const title = `${baseTitle} ${Math.random().toString(36).slice(2, 7)}`;
	const threadId = await createSession(request, title);
	const roots = await request.get('/api/files/tree', { params: { token: TEST_TOKEN } });
	const workspace = (await roots.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path;
	const artifacts: Artifact[] = [];
	for (let index = 1; index <= 9; index++) {
		const name = `${title} artifact ${index} enough words to wrap.md`;
		const artifactPath = path.join(workspace, name);
		const saved = await request.put('/api/files/content', {
			params: { token: TEST_TOKEN },
			data: { path: artifactPath, text: `# Artifact ${index}\n`, thread_id: threadId, create: true },
		});
		expect(saved.ok(), await saved.text()).toBeTruthy();
		artifacts.push({ threadId, title, name, path: artifactPath, artifactId: (await saved.json()).artifact.id as string });
	}
	const message = artifacts.map((artifact) => `[${artifact.name}](${encodeURI(artifact.path)})`).join('\n');
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !state) throw new Error('The card fixture requires isolated E2E mock state');
	execFileSync(python(), ['-c', [
		'import sys, uuid',
		'from infra.database import get_db',
		'from thread.transcript_order import draw_transcript_position',
		'thread, content = sys.argv[1:]',
		'db = get_db()',
		'seq = draw_transcript_position(db, thread)',
		'db.execute("INSERT INTO messages (id, thread_id, role, content, metadata, created_at, seq) VALUES (?, ?, ?, ?, ?, datetime(\'now\'), ?)", (uuid.uuid4().hex, thread, "assistant", content, "{}", seq))',
		'db.commit()',
	].join('\n'), threadId, message], {
		cwd: backendDir,
		encoding: 'utf8',
		env: { ...process.env, CORSIA_TEST_MODE: '1', CORSIA_STATE_DIR: state, CORSIA_WORKSPACE: process.env.CORSIA_E2E_WORKSPACE ?? '' },
	});
	return artifacts[0];
}

function recordAsk(artifact: Artifact, { retire = false }: { retire?: boolean } = {}): void {
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !state) throw new Error('The card fixture requires isolated E2E mock state');
	execFileSync(python(), ['-c', [
		'import json, sys, uuid',
		'from infra.database import get_db',
		'from plans import service',
		'from plans.boundary import PlanActor, mutate',
		'from registry import models',
		'from thread.transcript_order import draw_transcript_position',
		'thread, artifact, name, retired = sys.argv[1:]',
		'db = get_db()',
		'user = db.execute("SELECT user_id FROM threads WHERE id=?", (thread,)).fetchone()["user_id"]',
		'plan = service.ensure_plan("thread", thread, db=db)',
		'node = service.add_node(plan.id, kind="task", title="Review the rollout", acceptance_criteria="Reviewed", db=db)',
		'db.commit()',
		'turn, call = uuid.uuid4().hex, uuid.uuid4().hex',
		'seq = draw_transcript_position(db, thread)',
		'metadata = json.dumps({"turn_id": turn, "tool_calls": [{"id": call, "type": "function", "function": {"name": "plan_update", "arguments": "{}"}}]})',
		'db.execute("INSERT INTO messages (id, thread_id, role, content, metadata, created_at, seq) VALUES (?, ?, ?, ?, ?, datetime(\'now\'), ?)", (uuid.uuid4().hex, thread, "assistant", "Please review " + name, metadata, seq))',
		'mutate(plan.id, "block_node", {"node_id": node.id, "reason_key": "review", "reason": "Review the artifact", "owner_kind": "user", "owner_id": None, "ask_artifact_id": artifact, "expected_revision": service._node_row(db, node.id)["revision"]}, actor=PlanActor(thread, user, None, turn_id=turn, provider_call_id=call), provenance="e2e.chat_card", mutation_id=uuid.uuid4().hex, db=db)',
		'db.commit()',
		'if retired == "1":',
		'    models.retire(artifact, tombstone=True, db=db)',
		'    db.commit()',
	].join('\n'), artifact.threadId, artifact.artifactId, artifact.name, retire ? '1' : '0'], {
		cwd: backendDir,
		encoding: 'utf8',
		env: { ...process.env, CORSIA_TEST_MODE: '1', CORSIA_STATE_DIR: state, CORSIA_WORKSPACE: process.env.CORSIA_E2E_WORKSPACE ?? '' },
	});
}

async function setFocusMode(request: APIRequestContext, enabled: boolean): Promise<void> {
	const updated = await request.patch('/api/settings', {
		headers: { Authorization: `Bearer ${TEST_TOKEN}` },
		data: { features: { focus_mode: enabled } },
	});
	expect(updated.ok(), await updated.text()).toBeTruthy();
}

type Surface = readonly [label: string, width: number, theme: 'light' | 'dark'];
const SURFACES: readonly Surface[] = [
	['1440 light', 1440, 'light'],
	['1440 dark', 1440, 'dark'],
	['393 light', 393, 'light'],
	['393 dark', 393, 'dark'],
];

async function openTranscript(page: Page, artifact: Artifact, width: number, theme: 'light' | 'dark'): Promise<void> {
	await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
	await page.addInitScript((selectedTheme) => localStorage.setItem('corsia_theme', selectedTheme), theme);
	await loginByUrl(page, { waitForComposer: false });
	if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').filter({ hasText: artifact.title }).click();
	await expect(page.locator(`[data-artifact-id="${artifact.artifactId}"]`)).toBeVisible();
}

function card(page: Page, artifact: Artifact) {
	return page.locator(`[data-artifact-id="${artifact.artifactId}"]`);
}

async function screenshot(page: Page, name: string): Promise<void> {
	const directory = process.env.CORSIA_E2E_EVIDENCE_DIR;
	if (!directory) return;
	fs.mkdirSync(directory, { recursive: true });
	await page.screenshot({ path: path.join(directory, `${name}.png`), animations: 'disabled' });
}

for (const [label, width, theme] of SURFACES) {
	test.describe(label, () => {
		if (width === 393) test.use({ hasTouch: true, isMobile: true });
		test(`@surface:chat-cards a merged ask opens its artifact and flips immediately (${label})`, async ({ page, request }, info) => {
			test.skip(width !== 393 && info.project.name === 'chromium-mobile', 'desktop is proved by the desktop project');
			await setFocusMode(request, true);
			const artifact = await seedArtifact(request, `Chat card ${label}`);
			recordAsk(artifact);
			await openTranscript(page, artifact, width, theme);
			const row = card(page, artifact);
			await expect(row).toHaveAttribute('data-card-state', 'needs_review');
			await expect(row).toContainText('Needs your review');
			await screenshot(page, `chat-card-${width}-${theme}`);

			let release!: () => void;
			const held = new Promise<void>((resolve) => { release = resolve; });
			await page.route('**/api/attention/*/clear', async (route) => { await held; await route.continue(); });
			await row.click();
			const reviewBar = page.locator('.review-bar');
			await expect(reviewBar).toBeVisible();
			await reviewBar.getByRole('button', { name: 'Approve', exact: true }).click();
			await expect(row).toHaveAttribute('data-card-state', 'approved');
			await screenshot(page, `chat-card-answer-flip-${width}-${theme}`);
			release();
			await expect(reviewBar.locator('.rb-receipt')).toContainText('You approved this');
		});

		test(`@surface:chat-cards an image ask has a thumbnail and an unavailable artifact is not tappable (${label})`, async ({ page, request }, info) => {
			test.skip(width !== 393 && info.project.name === 'chromium-mobile', 'desktop is proved by the desktop project');
			await setFocusMode(request, true);
			const image = await seedArtifact(request, `Chat image ${label}`, true);
			recordAsk(image);
			await openTranscript(page, image, width, theme);
			const imageCard = card(page, image);
			await expect(imageCard.locator('img.thumb')).toHaveAttribute('src', /\/api\/files\/content/);
			await screenshot(page, `chat-card-image-${width}-${theme}`);
			await imageCard.click();
			await expect(page.locator('.review-bar')).toBeVisible();

			await setFocusMode(request, false);
			const unavailable = await seedArtifact(request, `Chat unavailable ${label}`);
			recordAsk(unavailable, { retire: true });
			await openTranscript(page, unavailable, width, theme);
			const unavailableCard = card(page, unavailable);
			await expect(unavailableCard).toHaveClass(/gone/);
			await expect(unavailableCard).toContainText('No longer available');
			expect(await unavailableCard.evaluate((element) => element.tagName)).toBe('DIV');
			await screenshot(page, `chat-card-unavailable-${width}-${theme}`);
		});
	});
}

for (const theme of ['light', 'dark'] as const) {
	test.describe(`393 ${theme} evidence states`, () => {
		test.use({ hasTouch: true, isMobile: true });

		test(`@surface:chat-cards nine chips retain their phone cue and wrap on desktop (${theme})`, async ({ page, request }, info) => {
			test.skip(info.project.name !== 'chromium-mobile', 'phone is proved by the mobile project');
			await setFocusMode(request, false);
			const artifacts = await seedChipArtifacts(request, `Chat chips ${theme}`);
			await openTranscript(page, artifacts, 393, theme);
			const chips = page.locator('.chips');
			await expect(chips).toHaveAttribute('data-more', '');
			await screenshot(page, `chat-chips-393-${theme}-unscrolled`);
			await chips.evaluate((node) => { node.scrollLeft = Math.round((node.scrollWidth - node.clientWidth) / 2); });
			await expect(chips).toHaveAttribute('data-before', '');
			await expect(chips).toHaveAttribute('data-more', '');
			await screenshot(page, `chat-chips-393-${theme}-scrolled`);

			await page.setViewportSize({ width: 1440, height: 900 });
			await expect(chips.locator('li')).toHaveCount(9);
			await expect(chips.getByRole('button', { name: '+1 more' })).toBeVisible();
			await screenshot(page, `chat-chips-1440-${theme}-wrapped`);
		});

		test(`@surface:chat-cards settled counts show and yield at the phone width (${theme})`, async ({ page, request }, info) => {
			test.skip(info.project.name !== 'chromium-mobile', 'phone is proved by the mobile project');
			await setFocusMode(request, true);
			const artifact = await seedArtifact(request, `Chat settled ${theme}`);
			recordAsk(artifact);
			await page.route('**/api/attention/*/clear', (route) => route.fulfill({
				status: 200,
				contentType: 'application/json',
				body: JSON.stringify({ review: { verdict: 'approved_with_changes', note: null, feedback_counts: { comments: 2, suggestions: 1, edits: 0 } } }),
			}));
			await openTranscript(page, artifact, 393, theme);
			const row = card(page, artifact);
			await row.click();
			await page.locator('.review-bar').getByRole('button', { name: 'Approve', exact: true }).click();
			await expect(row).toHaveAttribute('data-card-state', 'approved_with_changes');
			await expect(row).toContainText('2 comments, 1 suggestion');
			await screenshot(page, `chat-card-settled-counts-${theme}`);
		});

		test(`@surface:chat-cards answer failure reverts the optimistic row (${theme})`, async ({ page, request }, info) => {
			test.skip(info.project.name !== 'chromium-mobile', 'phone is proved by the mobile project');
			await setFocusMode(request, true);
			const artifact = await seedArtifact(request, `Chat reverted ${theme}`);
			recordAsk(artifact);
			let release!: () => void;
			const held = new Promise<void>((resolve) => { release = resolve; });
			await page.route('**/api/attention/*/clear', async (route) => { await held; await route.fulfill({ status: 500, contentType: 'application/json', body: '{}' }); });
			await openTranscript(page, artifact, 393, theme);
			const row = card(page, artifact);
			await screenshot(page, `chat-card-failed-send-${theme}-before`);
			await row.click();
			await page.locator('.review-bar').getByRole('button', { name: 'Approve', exact: true }).click();
			await expect(row).toHaveAttribute('data-card-state', 'approved');
			await screenshot(page, `chat-card-failed-send-${theme}-during`);
			release();
			await expect(row).toHaveAttribute('data-card-state', 'needs_review');
			await screenshot(page, `chat-card-failed-send-${theme}-reverted`);
		});
	});
}
