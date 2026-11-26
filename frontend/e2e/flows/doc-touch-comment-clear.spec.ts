import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { raiseAsk } from '../support/reviewAsk';
import { inEditorView } from '../support/liveDocument';
import { seedLoaded, openLoaded } from '../support/loadedComments';
import { seed, open, settled } from '../support/reviewFixtures';

// The touch Comment button stays at the foot unless a control at the foot would be under it; then it sits just above that control.
// Each control is hit-tested at its centre and four corners: nothing the finger lands on may be the Comment holder's button.
test.use({ hasTouch: true, isMobile: true });
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const backendDir = path.join(repoRoot, 'backend');
const python = () => process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python');
function addPersonStep(threadId: string, artifactId: string): void {
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !state) throw new Error('The person-step fixture requires isolated E2E mock state');
	execFileSync(python(), ['-c', [
		'import sys, uuid',
		'from infra.database import get_db',
		'from plans import service',
		'from plans.boundary import PlanActor, mutate',
		'from infra.db_fts import _init_messages_fts',
		'from infra.input_admission import accept_thread_input',
		'thread, artifact = sys.argv[1:]',
		'db = get_db()',
		'user = db.execute("SELECT user_id FROM threads WHERE id=?", (thread,)).fetchone()["user_id"]',
		'intent = "Set up the workspace from the instructions."',
		'if db.execute("SELECT 1 FROM sqlite_master WHERE type=\'table\' AND name=\'messages_fts\'").fetchone() is None: _init_messages_fts(db)',
		'message_id, turn_id = uuid.uuid4().hex, uuid.uuid4().hex',
		'accept_thread_input(thread, role="user", content=intent, intent="apply_now", message_id=message_id, db=db)',
		'db.execute("UPDATE messages SET active_turn_id=? WHERE id=?", (turn_id, message_id))',
		'plan = service.ensure_plan("thread", thread, db=db)',
		'params = {"kind": "task", "title": "Set up the workspace", "acceptance_criteria": "It is ready.", "size": "unknown", "assignee": "user", "description": "Follow the document. Already tried: I opened the instructions.", "instructions_artifact_id": artifact, "intent": intent}',
		'mutate(plan.id, "add", params, actor=PlanActor(thread, user, None, turn_id=turn_id), provenance="e2e.person_step", mutation_id=uuid.uuid4().hex, db=db)',
		'db.commit()',
	].join('\n'), threadId, artifactId], {
		cwd: backendDir, encoding: 'utf8',
		env: { ...process.env, CORSIA_TEST_MODE: '1', CORSIA_STATE_DIR: state, CORSIA_WORKSPACE: process.env.CORSIA_E2E_WORKSPACE ?? '' },
	});
}


async function select(page: Page, words: string) {
	await inEditorView(page, (view, w) => {
		const at = view.state.doc.toString().indexOf(w);
		if (at < 0) throw new Error('passage missing');
		view.dispatch({ selection: { anchor: at, head: at + w.length }, effects: view.constructor.scrollIntoView(at, { y: 'center' }) });
	}, words);
	await expect(page.locator('.touch-comment-bar.offered .sel-comment')).toBeVisible();
	await settled(page);
}

/** What a finger lands on at the centre and near each corner of every visible control matching `selector`: the names of the points the Comment button covers. */
async function coveredPoints(page: Page, selector: string): Promise<{ controls: number; covered: string[] }> {
	return page.evaluate((sel) => {
		const covered: string[] = [];
		let controls = 0;
		for (const el of document.querySelectorAll<HTMLElement>(sel)) {
			const r = el.getBoundingClientRect();
			if (r.width === 0 || r.height === 0 || getComputedStyle(el).visibility === 'hidden') continue;
			controls += 1;
			// The corner points sit 6px in along both sides: the controls are rounded, and a point outside the curve belongs to nothing of theirs.
			const label = (el.getAttribute('aria-label') || el.textContent || sel).trim().slice(0, 24);
			const points: Array<[string, number, number]> = [
				['centre', r.x + r.width / 2, r.y + r.height / 2], ['top-left', r.x + 6, r.y + 6], ['top-right', r.right - 6, r.y + 6],
				['bottom-left', r.x + 6, r.bottom - 6], ['bottom-right', r.right - 6, r.bottom - 6],
			];
			for (const [name, x, y] of points) if (document.elementFromPoint(x, y)?.closest('.touch-comment-bar')) covered.push(`${label}: ${name}`);
		}
		return { controls, covered };
	}, selector);
}

const holderBottom = (page: Page) => page.locator('.touch-comment-bar').evaluate((el) => getComputedStyle(el).bottom);
const WIDTHS = [393, 320];

for (const width of WIDTHS) {
	test(`Comment clears the person-step bar at ${width}`, async ({ page, request }) => {
		test.setTimeout(240_000);
		const doc = await seed(request, 'Rollout plan');
		addPersonStep(doc.threadId, doc.artifactId);
		await open(page, doc, 'phone', 'light');
		await page.setViewportSize({ width, height: 851 }); await settled(page);
		await expect(page.locator('[data-ask-card]')).toHaveCount(1);
		await select(page, 'Step 3:');
		const rest = await coveredPoints(page, '[data-ask-card] button');
		expect(rest.controls).toBeGreaterThan(1);
		expect(rest.covered).toEqual([]);
		await page.locator('[data-ask-card] .rb-dock-ask').click(); await settled(page);
		await select(page, 'Step 3:');
		expect((await coveredPoints(page, '[data-ask-card] button')).covered).toEqual([]);
	});
	test(`Comment clears the review ask bar at ${width}`, async ({ page, request }) => {
		test.setTimeout(240_000);
		const doc = await seed(request, 'Rollout plan');
		raiseAsk(doc.threadId, doc.artifactId, 'Check the rollout plan before the ring opens.');
		await open(page, doc, 'phone', 'light');
		await page.setViewportSize({ width, height: 851 }); await settled(page);
		await expect(page.locator('[data-ask-card]')).toHaveCount(1);
		await select(page, 'Step 3:');
		const rest = await coveredPoints(page, '[data-ask-card] button');
		expect(rest.controls).toBeGreaterThan(1);
		expect(rest.covered).toEqual([]);
		await page.locator('[data-ask-card] .rb-dock-ask').click(); await settled(page);
		await select(page, 'Step 3:');
		expect((await coveredPoints(page, '[data-ask-card] button')).covered).toEqual([]);
	});
	test(`Comment stays at the foot with no bar showing at ${width}`, async ({ page, request }) => {
		test.setTimeout(240_000);
		const doc = await seed(request, 'Rollout plan');
		await open(page, doc, 'phone', 'light');
		await page.setViewportSize({ width, height: 851 }); await settled(page);
		await select(page, 'Step 3:');
		expect(await holderBottom(page)).toBe('0px');
	});
	test(`Comment clears the jump pill at ${width}`, async ({ page, request }) => {
		test.setTimeout(240_000);
		const loaded = await seedLoaded(request, 'Rollout plan');
		await openLoaded(page, loaded, { width, height: 851, theme: 'light', touch: true, pane: 'open' });
		await select(page, 'Launch review');
		await expect(page.locator('[data-doc-jump] button')).toBeVisible();
		const jump = await coveredPoints(page, '[data-doc-jump] button');
		expect(jump.controls).toBe(1);
		expect(jump.covered).toEqual([]);
		// Where the jump pill is not under the button's width (393), the button does not move for it.
		const overlaps = await page.evaluate(() => {
			const a = document.querySelector('.touch-comment-bar .sel-comment')!.getBoundingClientRect(), b = document.querySelector('[data-doc-jump] button')!.getBoundingClientRect();
			return a.left < b.right && b.left < a.right;
		});
		if (!overlaps) expect(await holderBottom(page)).toBe('0px');
	});
}
