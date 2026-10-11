import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, test, type APIRequestContext, type Page, type TestInfo } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { syntheticPng } from '../support/syntheticImages';

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
	return { threadId, title, name, docPath, text, artifactId: (await seeded.json()).artifact.id as string };
}
type Doc = Awaited<ReturnType<typeof seed>>;

/** A registered image the owning thread asks about; the same fixture shape as a document, with no text. */
async function seedImage(request: APIRequestContext, title: string): Promise<Doc> {
	const threadId = await createSession(request, title);
	const roots = await request.get('/api/files/tree', { params: { token: TEST_TOKEN } });
	const workspace = (await roots.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path;
	const name = `${title} ${Date.now()}.png`, docPath = path.join(workspace, name);
	fs.writeFileSync(docPath, syntheticPng(640, 400));
	const registered = await request.post('/api/files/register', { params: { token: TEST_TOKEN }, data: { path: docPath, thread_id: threadId } });
	expect(registered.ok(), await registered.text()).toBeTruthy();
	const artifact = (await registered.json()).artifact;
	expect(artifact.type).toBe('image');
	return { threadId, title, name, docPath, text: '', artifactId: artifact.id as string };
}

/** A comment on the real text of "Step <n>", anchored the way the editor anchors a selection. */
async function comment(request: APIRequestContext, doc: Doc, body: string, step: number) {
	const from = doc.text.indexOf(`Step ${step}:`), to = from + 'Step'.length;
	expect(from).toBeGreaterThan(-1);
	const created = await request.post(`/api/artifacts/${doc.artifactId}/comments`, {
		params: { token: TEST_TOKEN }, data: { body, anchor_text: 'Step', from_off: from, to_off: to },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
}

/**
 * The three surfaces the bar is proved on. `default` is a desktop user's first layout: the plan pane open
 * and the default split, so the document canvas is under 600px and the bar stacks, with compact buttons
 * under a fine pointer. `closed` closes the pane for the one-row layout. `phone` emulates a touch screen,
 * where the stacked buttons are 44px.
 */
type Mode = 'default' | 'closed' | 'phone';
const WIDTH: Record<Mode, number> = { default: 1440, closed: 1440, phone: 393 };

async function open(page: Page, doc: Doc, mode: Mode, theme: 'light' | 'dark') {
	await page.setViewportSize({ width: WIDTH[mode], height: mode === 'phone' ? 852 : 900 });
	await page.addInitScript(({ doc, theme, wide }) => {
		localStorage.setItem('corsia_theme', theme);
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [doc.threadId]: {
			tabs: [{ instanceId: doc.threadId, artifactId: doc.artifactId, path: doc.docPath, title: doc.name, loadedRevision: null, latestRevision: null, status: 'loading' }],
			activeTabId: doc.threadId, visible: true, activeSurface: 'artifact',
		} }, splitWidth: wide ? 700 : null }));
	}, { doc, theme, wide: mode === 'closed' });
	await loginByUrl(page, { waitForComposer: false });
	await selectThread(page, doc, mode);
	// The plan pane opens beside the document by default; the one-row layout needs the canvas's full width.
	if (mode === 'closed' && !(await page.locator('.pane').evaluate((pane) => pane.classList.contains('collapsed')))) {
		await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
		await expect(page.locator('.pane.collapsed')).toBeVisible();
	}
}
async function selectThread(page: Page, doc: Doc, mode: Mode) {
	const image = doc.name.endsWith('.png');
	if (mode === 'phone') await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').filter({ hasText: doc.title }).click();
	if (image) await expect(page.getByRole('img', { name: doc.name })).toBeVisible();
	else await expect(page.locator('.cm-content')).toContainText('Step 1:');
}

const bar = (page: Page) => page.locator('.review-bar');
/** The bar's geometry per surface: one compact row, a stacked bar with compact right-aligned buttons, or 44px touch buttons. */
async function expectLayout(page: Page, mode: Mode) {
	const canvas = () => page.locator('.document-canvas, .image-canvas').first().evaluate((el) => el.getBoundingClientRect().width);
	if (mode === 'closed') await expect.poll(canvas).toBeGreaterThan(600);
	else if (mode === 'default') expect(await canvas(), 'the default layout is a narrow canvas').toBeLessThan(600);
	const box = (await bar(page).boundingBox())!;
	const buttons = await bar(page).locator('.btn').evaluateAll((els) =>
		els.map((el) => { const r = el.getBoundingClientRect(); return { h: r.height, w: r.width, right: r.right }; }));
	expect(buttons.length).toBeGreaterThanOrEqual(2);
	if (mode === 'closed') {
		await expect(bar(page).locator('.rb-narrow')).toBeHidden();
		expect(box.height, 'one row').toBeLessThanOrEqual(52);
	}
	if (mode === 'phone') {
		for (const button of buttons) expect(button.h, 'touch target').toBeGreaterThanOrEqual(44);
		expect(box.width).toBeLessThanOrEqual(WIDTH.phone);
		return;
	}
	for (const button of buttons) {
		expect(button.h, 'compact header-style button').toBeLessThanOrEqual(32);
		expect(button.w, 'never stretched to half the bar').toBeLessThan(box.width * 0.45);
	}
	expect(box.x + box.width - Math.max(...buttons.map((button) => button.right)), 'right-aligned').toBeLessThanOrEqual(24);
}
const shot = (page: Page, name: string, info: TestInfo) =>
	page.screenshot({ path: info.outputPath(`${name}.png`), animations: 'disabled' });

type Case = readonly [label: string, mode: Mode, theme: 'light' | 'dark'];
type Run = (args: { page: Page; request: APIRequestContext; info: TestInfo; mode: Mode; theme: 'light' | 'dark' }) => Promise<void>;
/** One test per case; a phone case runs with a touch screen so `(pointer: coarse)` applies. */
function journey(name: string, cases: readonly Case[], run: Run) {
	for (const [label, mode, theme] of cases) {
		test.describe(label, () => {
			if (mode === 'phone') test.use({ hasTouch: true, isMobile: true });
			test(`@surface:review-bar ${name} (${label})`, async ({ page, request }, info) => run({ page, request, info, mode, theme }));
		});
	}
}
const ALL: readonly Case[] = [
	['1440 light', 'default', 'light'], ['1440 dark', 'default', 'dark'],
	['1440 light pane closed', 'closed', 'light'], ['393 light', 'phone', 'light'],
];

journey('approve a plain ask from the reading', ALL, async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review plain ${mode} ${theme}`);
	raiseAsk(doc.threadId, doc.artifactId, 'Is the rollout plan right?');
	await open(page, doc, mode, theme);
	await expect(bar(page)).toBeVisible();
	await expect(bar(page)).toContainText('Is the rollout plan right?');
	await expect(bar(page).getByRole('button', { name: 'Approve', exact: true })).toBeVisible();
	await expectLayout(page, mode);
	await shot(page, 'ask', info);
	await bar(page).getByRole('button', { name: 'Approve', exact: true }).click();
	const receipt = bar(page).locator('.rb-receipt');
	await expect(receipt).toContainText('You approved this');
	await expect(receipt).toBeFocused();
	await shot(page, 'receipt', info);
	await page.reload();
	await selectThread(page, doc, mode);
	await expect(bar(page)).toHaveCount(0);
});

journey('approve with changes after two comments', ALL, async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review changes ${mode} ${theme}`);
	raiseAsk(doc.threadId, doc.artifactId, 'Check the ring order.');
	await comment(request, doc, 'Ring two should wait.', 2);
	await comment(request, doc, 'Add a rollback line.', 5);
	await open(page, doc, mode, theme);
	const approve = bar(page).getByRole('button', { name: 'Approve with changes', exact: true });
	await expect(approve).toBeVisible();
	await expectLayout(page, mode);
	await expect(page.getByText('Place again')).toHaveCount(0);
	// One primary on screen: the status row's Send stays secondary while the bar's Approve is primary.
	const send = page.locator('.document-status-row').getByRole('button', { name: /^Send 2 comments/ });
	await expect(send).toBeVisible();
	await expect(send).not.toHaveClass(/primary/);
	await expect(approve).toHaveClass(/primary/);
	await shot(page, 'ask-with-changes', info);
	await approve.click();
	await expect(bar(page).locator('.rb-receipt')).toContainText('You approved this with changes');
	await expect(bar(page).locator('.rb-receipt')).toContainText('sent 2 comments');
	await expect(page.locator('.document-status-row').getByRole('button', { name: /^Send \d+ comment/ })).toHaveCount(0);
	await expect(page.getByText('Place again')).toHaveCount(0);
	await shot(page, 'receipt-with-changes', info);
});

journey('request changes with only a note', [['1440 light', 'default', 'light'], ['393 light', 'phone', 'light']], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review note ${mode} ${theme}`);
	raiseAsk(doc.threadId, doc.artifactId, 'Is this ready to ship?');
	await open(page, doc, mode, theme);
	await bar(page).getByRole('button', { name: 'Request changes', exact: true }).click();
	const note = bar(page).getByRole('textbox');
	await expect(note).toBeFocused();
	const send = bar(page).getByRole('button', { name: 'Request changes', exact: true });
	await expect(send).toBeDisabled();
	await note.fill('Not yet: the rollback is missing.');
	await expectLayout(page, mode);
	await shot(page, 'noting', info);
	await send.click();
	await expect(bar(page).locator('.rb-receipt')).toContainText('You requested changes');
	await shot(page, 'receipt-requested', info);
	const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
	expect(overflow).toBeLessThanOrEqual(0);
});

journey('answer a plain ask on an image', [['1440 light', 'default', 'light'], ['393 light', 'phone', 'light']], async ({ page, request, info, mode, theme }) => {
	const doc = await seedImage(request, `Review image ${mode} ${theme}`);
	raiseAsk(doc.threadId, doc.artifactId, 'Is this the right diagram?');
	await open(page, doc, mode, theme);
	await expect(bar(page)).toContainText('Is this the right diagram?');
	// An image takes no comments, so Approve is always plain and Request changes always needs a note.
	await expect(bar(page).getByRole('button', { name: 'Approve', exact: true })).toBeVisible();
	await expect(bar(page).getByRole('button', { name: 'Approve with changes' })).toHaveCount(0);
	await expectLayout(page, mode);
	await shot(page, 'image-ask', info);
	await bar(page).getByRole('button', { name: 'Request changes', exact: true }).click();
	const send = bar(page).getByRole('button', { name: 'Request changes', exact: true });
	await expect(send).toBeDisabled();
	await bar(page).getByRole('textbox').fill('Use the new palette.');
	await shot(page, 'image-noting', info);
	await send.click();
	await expect(bar(page).locator('.rb-receipt')).toContainText('You requested changes');
	await expect(page.getByRole('img', { name: doc.name })).toBeVisible();
	await shot(page, 'image-receipt', info);
	const overflow = await page.evaluate(() => document.documentElement.scrollWidth - document.documentElement.clientWidth);
	expect(overflow).toBeLessThanOrEqual(0);
});

journey('a note in flight keeps its layout', [['1440 light', 'default', 'light'], ['393 light', 'phone', 'light']], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review sending ${mode} ${theme}`);
	raiseAsk(doc.threadId, doc.artifactId, 'Is this ready to ship?');
	let release!: () => void;
	const held = new Promise<void>((resolve) => { release = resolve; });
	await page.route('**/api/attention/*/clear', async (route) => { await held; await route.continue(); });
	await open(page, doc, mode, theme);
	await bar(page).getByRole('button', { name: 'Request changes', exact: true }).click();
	const note = bar(page).getByRole('textbox');
	await note.fill('Not yet: the rollback is missing.');
	await bar(page).getByRole('button', { name: 'Request changes', exact: true }).click();

	// Mid-send the bar is still the note layout: the same input, only the sender busy, nothing else pressable.
	await expect(bar(page).locator('.spinner')).toHaveCount(1);
	await expect(note).toBeVisible();
	await expect(note).toHaveValue('Not yet: the rollback is missing.');
	await expect(note).toHaveJSProperty('readOnly', true);
	await expect(bar(page).getByRole('button', { name: 'Approve' })).toHaveCount(0);
	await expect(bar(page).getByRole('button', { name: 'Cancel' })).toBeDisabled();
	await expect(bar(page).getByRole('status').filter({ hasText: 'Sending your review' })).toHaveCount(1);
	// The buttons ease into their disabled look; measure once that has settled.
	await page.waitForTimeout(400);
	await expectLayout(page, mode);
	await shot(page, 'note-sending', info);
	release();
	await expect(bar(page).locator('.rb-receipt')).toContainText('You requested changes');
});

journey('a long agent name never hides the ask', [['1440 light pane closed', 'closed', 'light']], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Review long ${mode} ${theme} Platform Reliability Engineering Operations Release Coordination Agent`);
	const reason = 'Is the rollout plan right for every region before we promote it?';
	raiseAsk(doc.threadId, doc.artifactId, reason);
	await open(page, doc, mode, theme);
	await expectLayout(page, mode);
	const line = bar(page).locator('.rb-line');
	const ask = bar(page).locator('.rb-wide .rb-ask');
	await expect(ask).toBeVisible();
	// What shows is the part of the ask inside the clipped line, not its full unclipped box.
	const visible = await ask.evaluate((el) => {
		const box = el.getBoundingClientRect(), edge = el.closest('.rb-line')!.getBoundingClientRect().right;
		return Math.max(0, Math.min(box.right, edge) - box.left);
	});
	expect(visible, 'the ask keeps a readable minimum next to a long name').toBeGreaterThanOrEqual(100);
	await shot(page, 'long-name-closed', info);

	// Keyboard focus unclamps the whole line in place, so the full ask is readable without a hover title.
	const closedHeight = (await bar(page).boundingBox())!.height;
	for (let step = 0; step < 12 && !(await line.evaluate((el) => el === document.activeElement)); step += 1) await page.keyboard.press('Tab');
	await expect(line).toBeFocused();
	await expect(line).toHaveCSS('white-space', 'normal');
	await expect(line).toHaveCSS('overflow', 'visible');
	await expect(ask).toHaveText(reason);
	expect(await line.evaluate((el) => el.scrollWidth <= el.clientWidth + 1), 'no horizontal clipping once focused').toBe(true);
	expect((await bar(page).boundingBox())!.height).toBeGreaterThan(closedHeight);
	await shot(page, 'long-name-focused', info);
});
