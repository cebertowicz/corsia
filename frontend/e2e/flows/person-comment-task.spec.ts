import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN } from '../support/corsia';

const repo = path.resolve(process.cwd(), '..');

async function settled(page: import('@playwright/test').Page) {
	await expect.poll(() => page.evaluate(() => document.getAnimations().filter((animation) =>
		animation.playState === 'running' && animation.effect?.getComputedTiming().iterations !== Infinity,
	).length)).toBe(0);
	await expect(page.getByText('Loading plan…')).toHaveCount(0);
}

for (const width of [1440, 900, 393]) test(`comment task counts and dismissal at ${width}px`, async ({ page, request }, testInfo) => {
	test.setTimeout(180_000);
	await page.setViewportSize({ width, height: 900 });
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const state = process.env.CORSIA_E2E_STATE_DIR;
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	if (!workspace || !state || !python || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Requires disposable mock environment');
	const title = `Comment task ${Date.now()}`;
	const threadId = await createSession(request, title);
	const folder = path.join(workspace, 'comment-task-evidence');
	mkdirSync(folder, { recursive: true });
	const docFolder = path.join(folder, String(width));
	mkdirSync(docFolder, { recursive: true });
	const docPath = path.join(docFolder, 'Launch plan.md');
	const query = new URLSearchParams({ token: TEST_TOKEN });
	const saved = await request.put(`/api/files/content?${query}`, { data: { path: docPath, text: 'Launch plan details for review.', thread_id: threadId, create: true } });
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	const secondSaved = await request.put(`/api/files/content?${query}`, { data: { path: path.join(docFolder, 'Second plan.md'), text: 'More details for review.', thread_id: threadId, create: true } });
	expect(secondSaved.ok(), await secondSaved.text()).toBeTruthy();
	const secondArtifactId = (await secondSaved.json()).artifact.id as string;
	execFileSync(python, ['-c', [
		'import runpy, sys',
		'runpy.run_path("frontend/e2e/support/focus-outcomes.py")["assert_disposable_roots"]()',
		'from infra.database import get_db',
		'from doc_feedback_helpers import add_comment',
		'db = get_db()',
		'root, _ = add_comment(db, sys.argv[1], author_kind="user", author_id=None, delivery_policy="document-held")',
		'add_comment(db, sys.argv[1], author_id=sys.argv[2], parent_id=root["id"])',
		'second_root, _ = add_comment(db, sys.argv[3], author_kind="user", author_id=None, delivery_policy="document-held")',
		'add_comment(db, sys.argv[3], author_id=sys.argv[2], parent_id=second_root["id"])',
		'from doceditor import comments, feedback_batches',
		'from infra.database import write_transaction',
		'from plans import service, artifact_links',
		'with write_transaction(db, scope="seed_agent_feedback") as connection:',
		'    plan = service.ensure_plan("thread", sys.argv[2], db=connection)',
		'    feedback = service.add_node(plan.id, kind="task", title="Author left 1 comment on Second plan", acceptance_criteria="Review feedback", actor_id=None, provenance="document.feedback_batch", db=connection)',
		'    artifact_links.link(feedback.id, sys.argv[3], "input", actor_id=None, provenance="document.feedback_batch", db=connection)',
		'    service.transition_node(feedback.id, "active", actor_id=sys.argv[2], db=connection)',
		'user_id = db.execute("SELECT user_id FROM threads WHERE id=?", (sys.argv[2],)).fetchone()[0]',
		'from infra.presence_records import enter',
		'from infra.time import utc_now',
		'enter(db, user_id, sys.argv[2], utc_now())',
		'with write_transaction(db, scope="seed_suggestion") as connection:',
		'    row, created = comments.create_with_status(sys.argv[1], body="Please review", author="Reviewer", author_kind="agent", author_thread_id=sys.argv[2], anchor_text="Launch plan", from_off=0, to_off=11, replacement="Revised launch plan", tagged_user_id=user_id, db=connection)',
		'    feedback_batches.record_comment(connection, row, created=created, delivery_policy=feedback_batches.IMMEDIATE)',
		'db.close()',
	].join('\n'), artifactId, threadId, secondArtifactId], { cwd: repo, env: { ...process.env, PYTHONPATH: 'backend:backend/tests', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state } });
	const queueResponse = await request.get(`/api/attention/queue?${query}`);
	expect(queueResponse.ok(), await queueResponse.text()).toBeTruthy();
	const queue = await queueResponse.json();
	const replyDocuments = new Set((queue.reply_items ?? []).filter((item: { detail: { source: { thread_id: string } } }) => item.detail.source.thread_id === threadId).map((item: { detail: { destination: { id: string } } }) => item.detail.destination.id));
	expect(replyDocuments.size).toBe(2);
	await openAppPastLanding(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	const sidebar = sidebarThread(page, title);
	const sidebarRow = page.locator('aside.sidebar').getByRole('listitem').filter({ hasText: title });
	await expect(sidebarRow.getByRole('button', { name: /Needs you/ })).toBeVisible();
	await settled(page);
	console.log(`build stamp ${width} orange: ${await page.locator('.version-overlay').innerText()}`);
	await page.screenshot({ path: testInfo.outputPath(`${width}-orange.png`) });
	const marked = await request.post(`/api/threads/${threadId}/seen?${query}`, { data: { unread: true }, headers: { Authorization: `Bearer ${TEST_TOKEN}` } });
	expect(marked.ok(), await marked.text()).toBeTruthy();
	const stateResponse = await request.get(`/api/plans/operator/thread-states?ids=${threadId}`, { headers: { Authorization: `Bearer ${TEST_TOKEN}` } });
	expect(stateResponse.ok()).toBeTruthy();
	expect((await stateResponse.json()).states[threadId].has_update).toBe(true);
	await expect(sidebarRow.getByRole('button', { name: /Needs you/ })).toBeVisible();
	await expect(sidebarRow.getByRole('button', { name: 'Update since your last visit' })).toHaveCount(0);
	console.log(`build stamp ${width} orange over unread: ${await page.locator('.version-overlay').innerText()}`);
	await page.screenshot({ path: testInfo.outputPath(`${width}-orange-over-unread.png`) });
	await sidebar.click();
	await expect(page.getByText(/\d+ new repl(?:y|ies).*\d+ documents/i)).toHaveCount(0);
	if (width > 1024) await expect(page.getByRole('list', { name: 'Plan steps' })).toBeVisible();
	await settled(page);
	console.log(`build stamp ${width} chat: ${await page.locator('.version-overlay').innerText()}`);
	await page.screenshot({ path: testInfo.outputPath(`${width}-chat-no-chip.png`) });
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: /^Open plan —/ }).click();
	else if (width <= 1024) await page.getByRole('button', { name: 'Toggle panel' }).click();
	const plan = page.getByRole('list', { name: 'Plan steps' });
	const row = plan.getByRole('listitem').filter({ hasText: 'Launch plan.md' }).first();
	await expect(row).toContainText('1 comment, 1 suggestion');
	await expect(plan.getByRole('listitem').filter({ hasText: 'Second plan.md' })).toHaveCount(0);
	const agentFeedback = plan.getByRole('listitem').filter({ hasText: 'Author left 1 comment on Second plan' });
	await expect(agentFeedback).toHaveCount(1);
	await expect(agentFeedback).toContainText('1 comment');
	await expect(agentFeedback).toContainText('Set aside');
	await settled(page);
	console.log(`build stamp ${width} task: ${await page.locator('.version-overlay').innerText()}`);
	await page.screenshot({ path: testInfo.outputPath(`${width}-comment-task.png`) });
	if (width === 393) {
		const box = await row.boundingBox();
		if (!box) throw new Error('Task row has no bounds');
		const session = await page.context().newCDPSession(page);
		const touchPoints = [{ x: box.x + box.width / 2, y: box.y + box.height / 2 }];
		await session.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints });
		await page.waitForTimeout(600);
		await session.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
		await session.detach();
	} else await row.click({ button: 'right' });
	const menu = page.getByRole('menu', { name: 'Actions for Launch plan.md' });
	await expect(page.getByRole('menuitem', { name: 'Dismiss all' })).toBeVisible();
	await expect.poll(() => menu.evaluate((element) => getComputedStyle(element).opacity)).toBe('1');
	const menuBox = await menu.boundingBox();
	expect(menuBox).not.toBeNull();
	expect(menuBox!.x).toBeGreaterThanOrEqual(0);
	expect(menuBox!.y).toBeGreaterThanOrEqual(0);
	expect(menuBox!.x + menuBox!.width).toBeLessThanOrEqual(width);
	expect(menuBox!.y + menuBox!.height).toBeLessThanOrEqual(900);
	console.log(`build stamp ${width} menu: ${await page.locator('.version-overlay').innerText()}`);
	await page.screenshot({ path: testInfo.outputPath(`${width}-comment-menu.png`) });
	await page.getByRole('menuitem', { name: 'Dismiss all' }).click();
	await expect(row).not.toContainText('1 comment, 1 suggestion');
	const comments = await request.get(`/api/artifacts/${artifactId}/comments?${query}`);
	expect(comments.ok()).toBeTruthy();
	const body = await comments.json();
	const suggestion = body.comments.find((item: { replacement: string | null }) => item.replacement === 'Revised launch plan');
	expect(suggestion?.decision).toBeFalsy();

	const seed = (mode: string) => execFileSync(python, ['frontend/e2e/support/person-comment-task-fixture.py', mode, artifactId, threadId], {
		cwd: repo, encoding: 'utf8', env: { ...process.env, PYTHONPATH: 'backend:backend/tests', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state },
	});
	const reopenPlan = async () => {
		await openAppPastLanding(page);
		if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await sidebarThread(page, title).click();
		if (width <= 768) await page.getByRole('button', { name: /^Open plan —/ }).click();
		else if (width <= 1024) await page.getByRole('button', { name: 'Toggle panel' }).click();
	};
	const capture = async (name: string) => {
		await settled(page);
		console.log(`build stamp ${width} ${name}: ${await page.locator('.version-overlay').innerText()}`);
		await page.screenshot({ path: testInfo.outputPath(`${width}-${name}.png`) });
	};
	seed('ask');
	await reopenPlan();
	const ask = page.getByRole('listitem').filter({ hasText: 'Review launch plan' }).first();
	await expect(ask).toBeVisible();
	await capture('ask-before-comments');
	const output = seed('feedback');
	const resultLine = output.split('\n').find((line) => line.startsWith('FIXTURE_RESULT='));
	if (!resultLine) throw new Error('Missing feedback fixture IDs');
	const ids = JSON.parse(resultLine.slice('FIXTURE_RESULT='.length)) as { replies: string[][]; suggestions: string[] };
	await reopenPlan();
	await expect(ask).toContainText('2 comments, 2 suggestions');
	await expect(ask.locator('.row-subline')).toHaveCSS('color', await ask.evaluate((element) => {
		const probe = document.createElement('span');
		probe.style.color = 'var(--accent)';
		element.appendChild(probe);
		const color = getComputedStyle(probe).color;
		probe.remove();
		return color;
	}));
	await expect(plan.getByRole('listitem').filter({ hasText: 'Launch plan.md' })).toHaveCount(0);
	await capture('ask-with-comments');
	const viewed = await request.post(`/api/artifacts/${artifactId}/comments/${ids.replies[0][0]}/viewed`, {
		headers: { Authorization: `Bearer ${TEST_TOKEN}` }, data: { seen_reply_id: ids.replies[0][1] },
	});
	expect(viewed.ok(), await viewed.text()).toBeTruthy();
	const decided = await request.post(`/api/comments/${ids.suggestions[0]}/decision?${query}`, { data: { decision: 'reject' } });
	expect(decided.ok(), await decided.text()).toBeTruthy();
	await reopenPlan();
	await expect(ask).toContainText('1 comment, 1 suggestion');
	await capture('ask-counts-reduced');
});
