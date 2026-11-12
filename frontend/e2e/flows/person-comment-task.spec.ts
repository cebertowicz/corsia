import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN } from '../support/corsia';
import { contrastBothThemes, countFit, settled, shoot } from '../support/comment-count-probe';

const repo = path.resolve(process.cwd(), '..');

for (const width of [1440, 900, 393]) test(`comment task counts and dismissal at ${width}px`, async ({ page, request }, testInfo) => {
	test.setTimeout(180_000);
	await page.setViewportSize({ width, height: 900 });
	const workspace = process.env.CORSIA_E2E_WORKSPACE;
	const state = process.env.CORSIA_E2E_STATE_DIR;
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	if (!workspace || !state || !python || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Requires disposable mock environment');
	const title = `Comment task ${Date.now()}`;
	const threadId = await createSession(request, title);
	const unreadTitle = `Unread only ${Date.now()}`;
	const unreadThreadId = await createSession(request, unreadTitle);
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
		'from doceditor import comments, feedback_batches',
		'from infra.database import write_transaction',
		'from plans import mutations, service',
		// The person's comments are sent the way the Send button sends them, so the agent holds a feedback task per
		// document before it replies. The agent finishes Launch plan's task; Second plan's stays open and covers that file.
		'def send(artifact):',
		'    root, sent = add_comment(db, artifact, author_kind="user", author_id=None)',
		'    [batch_id] = sent["batch_ids"]',
		'    feedback_batches.close_batch(db, batch_id, "send")',
		'    node_id = db.execute("SELECT delivered_node_id FROM doc_feedback_batches WHERE id=?", (batch_id,)).fetchone()[0]',
		'    assert node_id, batch_id',
		'    add_comment(db, artifact, author_id=sys.argv[2], parent_id=root["id"])',
		'    return node_id',
		'finished = send(sys.argv[1])',
		'send(sys.argv[3])',
		'with write_transaction(db, scope="finish_feedback") as connection:',
		'    mutations.request_review(finished, evidence="Answered on the document", owner_thread_id=sys.argv[2], actor_id=sys.argv[2], db=connection)',
		'    service.transition_node(finished, "accepted", actor_id=sys.argv[2], verification_result="passed", verified_by=sys.argv[2], db=connection)',
		'user_id = db.execute("SELECT user_id FROM threads WHERE id=?", (sys.argv[2],)).fetchone()[0]',
		'from infra.presence_records import enter',
		'from infra.time import utc_now',
		'enter(db, user_id, sys.argv[2], utc_now())',
		// Only an opened thread can be marked unread; the unread-only thread is entered once, as the person would.
		'enter(db, user_id, sys.argv[4], utc_now())',
		'with write_transaction(db, scope="seed_suggestion") as connection:',
		'    row, created = comments.create_with_status(sys.argv[1], body="Please review", author="Reviewer", author_kind="agent", author_thread_id=sys.argv[2], anchor_text="Launch plan", from_off=0, to_off=11, replacement="Revised launch plan", tagged_user_id=user_id, db=connection)',
		'    feedback_batches.record_comment(connection, row, created=created, delivery_policy=feedback_batches.IMMEDIATE)',
		'db.close()',
	].join('\n'), artifactId, threadId, secondArtifactId, unreadThreadId], { cwd: repo, env: { ...process.env, PYTHONPATH: 'backend:backend/tests', CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state } });
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
	await shoot(page, testInfo, `${width}-orange`);
	for (const id of [threadId, unreadThreadId]) {
		const marked = await request.post(`/api/threads/${id}/seen?${query}`, { data: { unread: true }, headers: { Authorization: `Bearer ${TEST_TOKEN}` } });
		expect(marked.ok(), await marked.text()).toBeTruthy();
	}
	const stateResponse = await request.get(`/api/plans/operator/thread-states?ids=${threadId},${unreadThreadId}`, { headers: { Authorization: `Bearer ${TEST_TOKEN}` } });
	expect(stateResponse.ok()).toBeTruthy();
	const states = (await stateResponse.json()).states;
	expect(states[threadId].has_update).toBe(true);
	expect(states[unreadThreadId].has_update).toBe(true);
	await expect(sidebarRow.getByRole('button', { name: /Needs you/ })).toBeVisible();
	await expect(sidebarRow.getByRole('button', { name: 'Update since your last visit' })).toHaveCount(0);
	const unreadRow = page.locator('aside.sidebar').getByRole('listitem').filter({ hasText: unreadTitle });
	await expect(unreadRow.getByRole('button', { name: 'Update since your last visit' })).toBeVisible();
	await expect(unreadRow.getByRole('button', { name: /Needs you/ })).toHaveCount(0);
	await shoot(page, testInfo, `${width}-orange-over-unread`);
	await sidebar.click();
	await expect(page.getByText(/\d+ new repl(?:y|ies).*\d+ documents/i)).toHaveCount(0);
	if (width > 1024) await expect(page.getByRole('list', { name: 'Plan steps' })).toBeVisible();
	await shoot(page, testInfo, `${width}-chat-no-chip`, true);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: /^Open plan —/ }).click();
	else if (width <= 1024) await page.getByRole('button', { name: 'Toggle panel' }).click();
	const plan = page.getByRole('list', { name: 'Plan steps' });
	const row = plan.getByRole('listitem').filter({ hasText: 'Launch plan.md' }).first();
	await expect(row).toContainText('1 comment, 1 suggestion');
	await expect(plan.getByRole('listitem').filter({ hasText: 'Second plan.md' })).toHaveCount(0);
	const agentFeedback = plan.getByRole('listitem').filter({ hasText: /left 1 comment on Second plan/ });
	await expect(agentFeedback).toHaveCount(1);
	await expect(agentFeedback).toContainText('1 comment');
	console.log(`agent feedback row ${width}: ${await agentFeedback.innerText()}`);
	await shoot(page, testInfo, `${width}-comment-task`, true);
	console.log(`file task ${width}: ${JSON.stringify(await countFit(row))}`);
	await contrastBothThemes(page, row, `file task ${width}`);
	console.log(`agent feedback row ${width}: ${JSON.stringify(await countFit(agentFeedback))}`);
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
	await shoot(page, testInfo, `${width}-comment-menu`);
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
	const reopenChat = async () => {
		await openAppPastLanding(page);
		if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await sidebarThread(page, title).click();
	};
	const countWhole = countFit;
	const card = page.locator('[data-person-step]').filter({ hasText: 'Review launch plan' }).first();
	const capture = (name: string, panel = true) => shoot(page, testInfo, `${width}-${name}`, panel);
	seed('ask');
	await reopenPlan();
	const ask = plan.getByRole('listitem').filter({ hasText: 'Review launch plan' }).first();
	await expect(ask).toBeVisible();
	await capture('ask-before-comments');
	const askSubtext = (await ask.locator('.row-subline').first().innerText()).trim();
	console.log(`ask subtext ${width}: ${askSubtext}`);
	const output = seed('feedback');
	const resultLine = output.split('\n').find((line) => line.startsWith('FIXTURE_RESULT='));
	if (!resultLine) throw new Error('Missing feedback fixture IDs');
	const ids = JSON.parse(resultLine.slice('FIXTURE_RESULT='.length)) as { replies: string[][]; suggestions: string[] };
	await reopenPlan();
	await expect(ask).toContainText('2 comments, 2 suggestions');
	// The ask keeps its whole subtext; a drawn ' · ', then the one dot against the accent count.
	if (askSubtext) await expect(ask.locator('.count-lead')).toHaveText(askSubtext);
	await expect(ask.locator('.comment-count > .count-sep')).toHaveText(' · ');
	await expect(ask.locator('.count-text > .comment-dot')).toHaveCount(1);
	await expect(ask.locator('.comment-dot')).toHaveCount(1);
	await expect(ask.locator('.count-text')).toHaveCSS('color', await ask.evaluate((element) => {
		const probe = document.createElement('span');
		probe.style.color = 'var(--accent-text)';
		element.appendChild(probe);
		const color = getComputedStyle(probe).color;
		probe.remove();
		return color;
	}));
	await contrastBothThemes(page, ask, `ask row ${width}`);
	await expect(plan.getByRole('listitem').filter({ hasText: 'Launch plan.md' })).toHaveCount(0);
	console.log(`ask row ${width}: ${JSON.stringify(await countWhole(ask))}`);
	await capture('ask-with-comments');
	// The ask's card in chat is the same ask: its state dot is the line's one dot; ' · ' and the accent count follow.
	if (width === 393) await reopenChat();
	await card.scrollIntoViewIfNeeded();
	await expect(card.locator('.count-text')).toHaveText('2 comments, 2 suggestions');
	await expect(card.locator('.comment-dot')).toHaveCount(0);
	await expect(card.locator('.comment-count > .count-sep')).toHaveText(' · ');
	await contrastBothThemes(page, card, `ask card ${width}`);
	await expect(card.getByRole('button', { name: /Needs you, 2 comments, 2 suggestions$/ })).toHaveCount(1);
	console.log(`ask card ${width}: ${JSON.stringify(await countWhole(card))}`);
	await capture('ask-card', false);
	const viewed = await request.post(`/api/artifacts/${artifactId}/comments/${ids.replies[0][0]}/viewed`, {
		headers: { Authorization: `Bearer ${TEST_TOKEN}` }, data: { seen_reply_id: ids.replies[0][1] },
	});
	expect(viewed.ok(), await viewed.text()).toBeTruthy();
	const decided = await request.post(`/api/comments/${ids.suggestions[0]}/decision?${query}`, { data: { decision: 'reject' } });
	expect(decided.ok(), await decided.text()).toBeTruthy();
	await reopenPlan();
	await expect(ask).toContainText('1 comment, 1 suggestion');
	console.log(`reduced row ${width}: ${JSON.stringify(await countWhole(ask))}`);
	await capture('ask-counts-reduced');

	const longCount = '12 comments, 3 suggestions';
	seed('long');
	await reopenPlan();
	await expect(ask.locator('.count-text')).toHaveText(longCount);
	console.log(`long row ${width}: ${JSON.stringify(await countWhole(ask))}`);
	if (width !== 393) {
		await expect(card.locator('.count-text')).toHaveText(longCount);
		console.log(`long card ${width}: ${JSON.stringify(await countWhole(card))}`);
	}
	await capture('long-count');
	if (width === 1440) {
		// The desktop Workspace panel has one fixed width; a narrower one is forced here to show the count wrapping.
		const pane = page.locator('aside.pane');
		await pane.evaluate((element) => element.style.setProperty('--pane-width', '240px'));
		await expect.poll(() => pane.evaluate((element) => Math.round(element.getBoundingClientRect().width))).toBe(240);
		const narrow = await countWhole(ask);
		console.log(`long row narrow panel ${width}: ${JSON.stringify(narrow)}`);
		expect(narrow.wrapped).toBe(true);
		await capture('long-count-narrow-panel', false);
	}
	if (width === 393) {
		await reopenChat();
		await card.scrollIntoViewIfNeeded();
		await expect(card.locator('.count-text')).toHaveText(longCount);
		console.log(`long card ${width}: ${JSON.stringify(await countWhole(card))}`);
		await capture('long-count-card');
	}
});
