import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type APIRequestContext, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';

/**
 * The in-view review bar (DCOL-FR12.4) on the real composed canvas: an agent's raised ask on an open
 * document, answered from the reading. The ask is raised through the plan service as the owning agent
 * thread (an agent's `block_node` with `ask_artifact_id`); everything the user does goes through the UI.
 */
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const backendDir = path.join(repoRoot, 'backend');
const python = () => process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python');

function raiseAsk(threadId: string, artifactId: string, reason: string): void {
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !state) throw new Error('The ask fixture requires isolated E2E mock state');
	execFileSync(python(), ['-c', [
		'import sys, uuid',
		'from infra.database import get_db',
		'from plans import service',
		'from plans.boundary import PlanActor, mutate',
		'thread, artifact, reason = sys.argv[1:]',
		'db = get_db()',
		'user = db.execute("SELECT user_id FROM threads WHERE id=?", (thread,)).fetchone()["user_id"]',
		'plan = service.ensure_plan("thread", thread, db=db)',
		'node = service.add_node(plan.id, kind="task", title="Review the proposal", acceptance_criteria="Reviewed", db=db)',
		'db.commit()',
		'mutate(plan.id, "block_node", {"node_id": node.id, "reason_key": "review", "reason": reason, "owner_kind": "user", "owner_id": None,',
		'  "ask_artifact_id": artifact, "expected_revision": service._node_row(db, node.id)["revision"]},',
		'  actor=PlanActor(thread, user, None), provenance="e2e.review_bar", mutation_id=uuid.uuid4().hex, db=db)',
		'db.commit()',
	].join('\n'), threadId, artifactId, reason], {
		cwd: backendDir, encoding: 'utf8',
		env: { ...process.env, CORSIA_TEST_MODE: '1', CORSIA_STATE_DIR: state, CORSIA_WORKSPACE: process.env.CORSIA_E2E_WORKSPACE ?? '' },
	});
}

async function seed(request: APIRequestContext, title: string) {
	const threadId = await createSession(request, title);
	const roots = await request.get('/api/files/tree', { params: { token: TEST_TOKEN } });
	const workspace = (await roots.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path;
	const name = `${title} ${Date.now()}.md`, docPath = path.join(workspace, name);
	const text = '# Rollout plan\n\n' + Array.from({ length: 12 }, (_, index) => `Step ${index + 1}: the rollout proceeds to the next ring.\n\n`).join('');
	const seeded = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: docPath, text, thread_id: threadId, create: true } });
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	return { threadId, title, name, docPath, artifactId: (await seeded.json()).artifact.id as string };
}
type Doc = Awaited<ReturnType<typeof seed>>;

async function comment(request: APIRequestContext, doc: Doc, body: string, from: number, to: number) {
	const created = await request.post(`/api/artifacts/${doc.artifactId}/comments`, {
		params: { token: TEST_TOKEN }, data: { body, anchor_text: 'Step', from_off: from, to_off: to },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
}

async function open(page: Page, doc: Doc, width: number, theme: 'light' | 'dark') {
	await page.setViewportSize({ width, height: width < 500 ? 852 : 900 });
	await page.addInitScript(({ doc, theme }) => {
		localStorage.setItem('corsia_theme', theme);
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [doc.threadId]: {
			tabs: [{ instanceId: doc.threadId, artifactId: doc.artifactId, path: doc.docPath, title: doc.name, loadedRevision: null, latestRevision: null, status: 'loading' }],
			activeTabId: doc.threadId, visible: true, activeSurface: 'artifact',
		} }, splitWidth: 500 }));
	}, { doc, theme });
	await loginByUrl(page, { waitForComposer: false });
	if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').filter({ hasText: doc.title }).click();
	await expect(page.locator('.cm-content')).toContainText('Step 1:');
}

const bar = (page: Page) => page.locator('.review-bar');
const shot = (page: Page, name: string, info: TestInfo) =>
	page.screenshot({ path: info.outputPath(`${name}.png`), animations: 'disabled' });

for (const [label, width, theme] of [['1440 light', 1440, 'light'], ['1440 dark', 1440, 'dark'], ['393 light', 393, 'light']] as const) {
	test(`@surface:review-bar approve a plain ask from the reading (${label})`, async ({ page, request }, info) => {
		const doc = await seed(request, `Review plain ${width} ${theme}`);
		raiseAsk(doc.threadId, doc.artifactId, 'Is the rollout plan right?');
		await open(page, doc, width, theme);
		await expect(bar(page)).toBeVisible();
		await expect(bar(page)).toContainText('Is the rollout plan right?');
		await expect(bar(page).getByRole('button', { name: 'Approve', exact: true })).toBeVisible();
		await shot(page, 'ask', info);
		await bar(page).getByRole('button', { name: 'Approve', exact: true }).click();
		const receipt = bar(page).locator('.rb-receipt');
		await expect(receipt).toContainText('You approved this');
		await expect(receipt).toBeFocused();
		await shot(page, 'receipt', info);
		await page.reload();
		if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.locator('.thread-item').filter({ hasText: doc.title }).click();
		await expect(page.locator('.cm-content')).toContainText('Step 1:');
		await expect(bar(page)).toHaveCount(0);
	});
}

for (const [label, width, theme] of [['1440 light', 1440, 'light'], ['1440 dark', 1440, 'dark'], ['393 light', 393, 'light']] as const) {
	test(`@surface:review-bar approve with changes after two comments (${label})`, async ({ page, request }, info) => {
		const doc = await seed(request, `Review changes ${width} ${theme}`);
		raiseAsk(doc.threadId, doc.artifactId, 'Check the ring order.');
		await comment(request, doc, 'Ring two should wait.', 18, 22);
		await comment(request, doc, 'Add a rollback line.', 60, 64);
		await open(page, doc, width, theme);
		const approve = bar(page).getByRole('button', { name: 'Approve with changes', exact: true });
		await expect(approve).toBeVisible();
		await shot(page, 'ask-with-changes', info);
		await approve.click();
		await expect(bar(page).locator('.rb-receipt')).toContainText('You approved this with changes');
		await expect(bar(page).locator('.rb-receipt')).toContainText('sent 2 comments');
		await shot(page, 'receipt-with-changes', info);
	});
}

for (const [label, width, theme] of [['1440 light', 1440, 'light'], ['393 light', 393, 'light']] as const) {
	test(`@surface:review-bar request changes with only a note (${label})`, async ({ page, request }, info) => {
		const doc = await seed(request, `Review note ${width} ${theme}`);
		raiseAsk(doc.threadId, doc.artifactId, 'Is this ready to ship?');
		await open(page, doc, width, theme);
		await bar(page).getByRole('button', { name: 'Request changes', exact: true }).click();
		const note = bar(page).getByRole('textbox');
		await expect(note).toBeFocused();
		const send = bar(page).getByRole('button', { name: 'Request changes', exact: true });
		await expect(send).toBeDisabled();
		await note.fill('Not yet: the rollback is missing.');
		await shot(page, 'noting', info);
		await send.click();
		await expect(bar(page).locator('.rb-receipt')).toContainText('You requested changes');
		await shot(page, 'receipt-requested', info);
		const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
		expect(overflow).toBeLessThanOrEqual(0);
	});
}
