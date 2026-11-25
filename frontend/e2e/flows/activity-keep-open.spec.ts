import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test, expect } from '../support/test';
import { openAppPastLanding, TEST_TOKEN } from '../support/corsia';
import { raiseAsk } from '../support/reviewAsk';
import { comment, seed } from '../support/reviewFixtures';
import { isolatedDb } from '../support/imageRegionComments';
import type { Page } from '@playwright/test';

const root = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const out = process.env.ACTIVITY_KEEP_OUT;
test.use({ seedPerTest: true });

function addTasks(thread: string, prefix: string, count: number, first: number) {
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !state) throw new Error('Requires isolated mock state');
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON!, ['-c', [
		'import sys', 'from infra.database import get_db', 'from plans import service',
		'thread, prefix, count, first = sys.argv[1:]', 'db = get_db()',
		'user = db.execute("SELECT user_id FROM threads WHERE id=?", (thread,)).fetchone()[0]',
		'plan = service.ensure_plan("thread", thread, db=db)',
		'for i in range(int(count)):',
		' node = service.add_node(plan.id, kind="task", title=f"{prefix} {i:02}", acceptance_criteria="Done", assignee="user", db=db)',
		' db.execute("INSERT INTO activity_ranks VALUES (?,?,?,?,?,?,?)", (user,"workspace",f"task:{node.id}",int(first)+i,"Test order",thread,"2026-01-01"))',
		'db.commit()',
	].join('\n'), thread, prefix, String(count), String(first)], {
		cwd: path.join(root, 'backend'), env: { ...process.env, CORSIA_TEST_MODE: '1', CORSIA_STATE_DIR: state, CORSIA_WORKSPACE: process.env.CORSIA_E2E_WORKSPACE ?? '' },
	});
}

async function activity(page: Page, width: number) {
	await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
	await openAppPastLanding(page);
	if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
}

for (const width of [1440, 393]) test(`approving a review keeps its unread document open at ${width}`, async ({ page, request }, info) => {
	 test.setTimeout(150_000);
	 const doc = await seed(request, 'Review with unread replies');
	 const rootId = await comment(request, doc, 'Why this step?', 1);
	 const reply = await request.post(`/api/artifacts/${doc.artifactId}/comments`, {
		 params: { token: TEST_TOKEN }, data: { body: 'Here is the explanation', parent_id: rootId, delivery_policy: 'immediate' },
	 });
	 expect(reply.ok()).toBeTruthy();
	 const replyId = (await reply.json()).comment.id as string;
	 isolatedDb([
		 'changed = connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Agent", sys.argv[2], sys.argv[3])).rowcount',
		 'assert changed == 1, changed',
	 ], doc.threadId, replyId);
	 raiseAsk(doc.threadId, doc.artifactId, 'Review this document');
	 await activity(page, width);
	 const review = page.locator('[data-activity-row]').filter({ hasText: 'Review this document' });
	 await expect(review).toBeVisible();
	 if (width === 393) await review.click();
	 const editor = page.locator('.pane-col .cm-content');
	 await expect(editor).toContainText('Step 1:');
	 await editor.evaluate((el) => { (window as any).__reviewEditor = el; });
	 let refreshedBeforeCallback = false;
	 await page.route(`**/api/attention/${doc.artifactId}/clear`, async (route) => {
		 const response = await route.fetch();
		 await expect(page.locator('[data-tab="notifications"][aria-label="Notifications, 1"]')).toHaveCount(1);
		 refreshedBeforeCallback = true;
		 await route.fulfill({ response });
	 });
	 await page.locator('.pane-col').getByRole('button', { name: 'Approve', exact: true }).click();
	 await expect.poll(() => refreshedBeforeCallback).toBe(true);
	 await expect.poll(async () => {
		 const response = await request.get('/api/attention/queue', { params: { token: TEST_TOKEN, view: 'activity' } });
		 const queue = await response.json();
		 return queue.notifications.items.some((row: { key: string }) => row.key === `doc:${doc.artifactId}`);
	 }).toBe(true);
	 await expect(editor).toContainText('Step 1:');
	 expect(await editor.evaluate((el) => el === (window as any).__reviewEditor)).toBe(true);
	 await expect(page.getByRole('region', { name: 'Replies to your comments' })).toBeVisible();
	 await page.screenshot({ path: info.outputPath('after-approve.png'), animations: 'disabled' });
});

for (const width of [1440, 393]) test(`replying keeps its now-read Activity row and pane at ${width}`, async ({ page, request }) => {
	test.setTimeout(150_000);
	page.setDefaultTimeout(12_000);
	const doc = await seed(request, 'Activity reply');
	const rootId = await comment(request, doc, 'Please explain this step', 1);
	const reply = await request.post(`/api/artifacts/${doc.artifactId}/comments`, {
		params: { token: TEST_TOKEN }, data: { body: 'Here is the explanation', parent_id: rootId, delivery_policy: 'immediate' },
	});
	expect(reply.ok(), await reply.text()).toBeTruthy();
	const replyId = (await reply.json()).comment.id as string;
	isolatedDb([
		'changed = connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Agent", sys.argv[2], sys.argv[3])).rowcount',
		'assert changed == 1, changed',
	], doc.threadId, replyId);
	addTasks(doc.threadId, 'Another item', 2, 10);
	await activity(page, width);
	await page.getByRole('tab', { name: /Notifications/ }).click();
	const row = page.locator('[data-activity-row]').filter({ hasText: doc.name });
	await expect(row).toBeVisible();
	const queueBefore = await (await request.get('/api/attention/queue', { params: { token: TEST_TOKEN, view: 'activity' } })).json();
	expect(queueBefore.notifications.items.some((item: { key: string }) => item.key === `doc:${doc.artifactId}`)).toBe(true);
	await row.click();
	const editor = page.locator('.pane-col .cm-content');
	await expect(editor).toContainText('Step 1:');
	const activityBar = page.getByRole('region', { name: 'Replies to your comments' });
	await expect(activityBar).toBeVisible();
	await expect(activityBar.getByRole('button', { name: 'Reply', exact: true })).toHaveCount(0);
	if (width === 393) {
		await page.getByRole('button', { name: 'Close comments' }).click();
		// Wait for the sheet's close transition before reopening (#3328).
		await expect(page.getByRole('dialog', { name: 'Document comments' })).toBeHidden();
		await page.locator('main').getByRole('button', { name: 'Notifications', exact: true }).click();
	}
	const top = (await row.boundingBox())!.y;
	const position = await row.evaluate((el) => [...document.querySelectorAll('[data-activity-row]')].indexOf(el));
	if (width === 393) {
		await row.click();
		// A restored landing does not mark a reply read; choosing its highlight does.
		await page.locator('.pane-col .cm-comment-anchor').first().click();
		await expect(page.getByRole('dialog', { name: 'Document comments' })).toBeVisible();
	}
	await expect(editor).toContainText('Step 1:');
	await editor.evaluate((el) => { (window as any).__replyEditor = el; });
	const body = (await editor.textContent()) ?? '';
	const card = page.getByRole('region', { name: /^Comment by/ }).filter({ hasText: 'Please explain this step' });
	const field = card.getByRole('textbox', { name: 'Reply' });
	if (width === 393) {
		await expect(card).toContainText('Please explain this step');
		await expect(field).toBeVisible();
		// The closing sheet can remain visible but inert until its replacement is ready.
		await expect.poll(() => field.evaluate((el) => !el.closest('[inert]'))).toBe(true);
	} else if (!await field.isVisible()) {
		await page.locator('.pane-col .cm-comment-anchor').first().click();
		await expect(field).toBeVisible();
	}
	await field.focus();
	await expect(field).toBeFocused();
	await field.fill('Thank you for explaining');
	await expect(field).toHaveValue('Thank you for explaining');
	await expect(editor).not.toContainText('Thank you for explaining');
	await expect.poll(async () => {
		const queue = await (await request.get('/api/attention/queue', { params: { token: TEST_TOKEN, view: 'activity' } })).json();
		return queue.notifications.items.some((item: { key: string }) => item.key === `doc:${doc.artifactId}`);
	}).toBe(false);
	const checkPane = async () => {
		await expect(editor).toContainText('Step 1:');
		expect(await editor.evaluate((el) => el === (window as any).__replyEditor)).toBe(true);
		expect(await editor.textContent(), 'the document body is unchanged').toBe(body);
		await expect(page.locator('.pane-col')).not.toContainText('No longer waiting on you.');
		await expect(row).toHaveAttribute('aria-current', 'true');
		expect(await row.evaluate((el) => [...document.querySelectorAll('[data-activity-row]')].indexOf(el))).toBe(position);
		await expect(page.locator('[data-activity-row][aria-current="true"]')).toHaveCount(1);
	};
	const checkRow = async () => {
		await expect(row).toBeVisible();
		expect(Math.abs((await row.boundingBox())!.y - top)).toBeLessThanOrEqual(3);
	};
	const frame = async (step: string) => { if (out) { fs.mkdirSync(out, { recursive: true }); await page.screenshot({ path: path.join(out, `reply-${step}-${width}.png`), animations: 'disabled' }); } };
	await checkPane();
	if (width === 393) {
		await page.getByRole('dialog', { name: 'Document comments' }).getByRole('button', { name: 'Close', exact: true }).click();
		// Wait for the sheet's close transition before reopening (#3328).
		await expect(page.getByRole('dialog', { name: 'Document comments' })).toBeHidden();
		await expect(field).toBeHidden();
	} else {
		await page.keyboard.press('Escape');
		await expect(field).toBeHidden();
	}
	await frame('opened');
	if (width === 393) {
		await page.locator('main').getByRole('button', { name: 'Notifications', exact: true }).click();
		await checkRow();
		await row.click();
	} else await checkRow();
	await checkPane();
	await page.locator('.pane-col .cm-comment-anchor').first().click();
	await expect(field).toBeVisible();
	await expect(field).toHaveValue('Thank you for explaining');
	await checkPane();
	await expect.poll(() => field.evaluate((el) => !el.closest('[inert]'))).toBe(true);
	await field.focus();
	await expect(field).toBeFocused();
	await field.fill('Thank you for explaining');
	const response = page.waitForResponse((r) => r.url().includes(`/api/artifacts/${doc.artifactId}/comments`) && r.request().method() === 'POST');
	await card.getByRole('button', { name: 'Send reply' }).click();
	expect((await response).ok()).toBeTruthy();
	await expect(card).toContainText('Thank you for explaining');
	await expect(field).toBeVisible();
	await checkPane();
	if (width === 393) {
		await page.getByRole('dialog', { name: 'Document comments' }).getByRole('button', { name: 'Close', exact: true }).click();
		// Wait for the sheet's close transition before reopening (#3328).
		await expect(page.getByRole('dialog', { name: 'Document comments' })).toBeHidden();
	} else await page.keyboard.press('Escape');
	await frame('sent');
	if (width === 393) await page.locator('main').getByRole('button', { name: 'Notifications', exact: true }).click();
	await checkRow();
	await page.getByRole('tab', { name: /Your tasks/ }).click();
	await page.locator('[data-activity-row]').filter({ hasText: 'Another item 00' }).click();
	if (width === 393) await page.locator('main').getByRole('button', { name: 'Your tasks', exact: true }).click();
	await page.getByRole('tab', { name: /Notifications/ }).click();
	await expect(row).toHaveCount(0);
	await frame('another');
});

test('approving the open review advances to what followed', async ({ page, request }) => {
	test.setTimeout(150_000);
	const first = await seed(request, 'Activity approve first');
	const second = await seed(request, 'Activity approve second');
	raiseAsk(first.threadId, first.artifactId, 'Approve this first document');
	raiseAsk(second.threadId, second.artifactId, 'Review this following document');
	await activity(page, 1440);
	await page.locator('[data-activity-row]').filter({ hasText: 'Approve this first document' }).click();
	await page.locator('.pane-col').getByRole('button', { name: 'Approve', exact: true }).click();
	await expect(page.locator('[data-activity-row][aria-current="true"]')).toContainText('Review this following document');
	await expect(page.locator('.pane-col')).not.toContainText('No longer waiting on you.');
});

for (const width of [1440, 393]) {
	test(`review draft remains after settlement at ${width}`, async ({ page, request }) => {
		test.setTimeout(150_000);
		const doc = await seed(request, `Activity draft ${width}`);
		const other = await seed(request, `Activity next ${width}`);
		const rootId = await comment(request, doc, 'An existing comment', 1);
		raiseAsk(doc.threadId, doc.artifactId, 'Review the document with the draft');
		raiseAsk(other.threadId, other.artifactId, 'Review the next document');
		await activity(page, width);
		await page.locator('[data-activity-row]').filter({ hasText: 'Review the document with the draft' }).click();
		await expect(page.locator('.pane-col .cm-content')).toContainText('Step 1:');
		await page.locator('.pane-col [data-comments-entry]').click();
		const card = page.locator(`[data-comment-id="${rootId}"]`).last();
		await expect(card).toBeVisible();
		const field = card.locator('textarea').first();
		await field.fill('A draft that must remain');
		await field.evaluate((el) => { (window as any).__activityDraft = el; });
		const read = await request.get(`/api/attention/${doc.artifactId}/review`, { params: { token: TEST_TOKEN } });
		expect(read.ok()).toBeTruthy();
		const holdIds = (await read.json()).hold_ids;
		const queueRead = page.waitForResponse((r) => r.url().includes('/api/attention/queue') && r.url().includes('view=activity'));
		const settled = await request.post(`/api/attention/${doc.artifactId}/clear`, { params: { token: TEST_TOKEN }, data: { token: TEST_TOKEN, outcome: 'answer', verdict: 'approved', hold_ids: holdIds } });
		expect(settled.ok(), await settled.text()).toBeTruthy();
		await queueRead;
		await page.waitForTimeout(300);
		if (out) { fs.mkdirSync(out, { recursive: true }); await page.screenshot({ path: path.join(out, `draft-${width}.png`), animations: 'disabled' }); }
		await expect(page.locator('.pane-col')).toContainText('No longer waiting on you.');
		await expect(field).toHaveValue('A draft that must remain');
		expect(await field.evaluate((el) => el === (window as any).__activityDraft)).toBe(true);
		await expect(page.locator('.pane-col .cm-content')).toContainText('Step 1:');
	});

	test(`inserting a higher row preserves open row position at ${width}`, async ({ page, request }) => {
		test.setTimeout(150_000);
		const doc = await seed(request, `Activity ordering ${width}`);
		addTasks(doc.threadId, 'Existing task', 15, 10);
		await activity(page, width);
		const open = page.locator('[data-activity-row]').filter({ hasText: 'Existing task 10' });
		await open.scrollIntoViewIfNeeded();
		await open.click();
		if (width === 393) {
			// The phone keeps the selected row in the hidden list while the pane is open.
			await page.locator('main').getByRole('button', { name: 'Your tasks', exact: true }).click();
		}
		await open.scrollIntoViewIfNeeded();
		const before = (await open.boundingBox())!.y;
		addTasks(doc.threadId, 'New highest task', 1, 1);
		const queueRead = page.waitForResponse((r) => r.url().includes('/api/attention/queue') && r.url().includes('view=activity'));
		await comment(request, doc, 'Refresh the queue', 1);
		await queueRead;
		await expect(page.locator('[data-activity-row]').filter({ hasText: 'New highest task' })).toHaveCount(1);
		await expect(open).toHaveAttribute('aria-current', 'true');
		expect(Math.abs((await open.boundingBox())!.y - before)).toBeLessThanOrEqual(1);
	});
}
