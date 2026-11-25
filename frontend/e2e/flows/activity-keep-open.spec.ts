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
	const row = page.locator('[data-activity-row]').filter({ hasText: 'Please explain this step' });
	await expect(row).toBeVisible();
	const queueBefore = await (await request.get('/api/attention/queue', { params: { token: TEST_TOKEN, view: 'activity' } })).json();
	expect(queueBefore.items.some((item: { key: string }) => item.key === `doc-reply:${doc.artifactId}:${rootId}`)).toBe(true);
	await row.click();
	const editor = page.locator('.pane-col .cm-content');
	await expect(editor).toContainText('Step 1:');
	if (width === 393) {
		await page.getByRole('button', { name: 'Close comments' }).click();
		await page.locator('main').getByRole('button', { name: 'Activity', exact: true }).click();
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
	const card = width === 393
		? page.getByRole('dialog', { name: 'Document comments' }).getByRole('region', { name: /^Comment by/ }).filter({ hasText: 'Please explain this step' })
		: page.locator(`[data-comment-id="${rootId}"]`).last();
	const field = card.locator('textarea').first();
	if (width === 393) {
		await expect(card).toContainText('Please explain this step');
		await expect(field).toBeVisible();
		// A sheet that is still closing is visible but not ready: its fields sit in an inert subtree, so a fill into one cannot take focus and the text goes to the focused document instead. Wait for the sheet that replaces it: no ancestor of the reply box is inert.
		await expect.poll(() => field.evaluate((el) => !el.closest('[inert]'))).toBe(true);
	} else if (!await field.isVisible()) await page.locator('.pane-col').getByRole('button', { name: 'Reply', exact: true }).click();
	await field.fill('Thank you for explaining');
	await expect.poll(async () => {
		const queue = await (await request.get('/api/attention/queue', { params: { token: TEST_TOKEN, view: 'activity' } })).json();
		return queue.items.some((item: { key: string }) => item.key === `doc-reply:${doc.artifactId}:${rootId}`);
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
		await expect(field).toBeHidden();
	} else await page.keyboard.press('Escape');
	await frame('opened');
	if (width === 393) {
		await page.locator('main').getByRole('button', { name: 'Activity', exact: true }).click();
		await checkRow();
		await row.click();
	} else await checkRow();
	await checkPane();
	if (!await field.isVisible()) await page.locator('.pane-col').getByRole('button', { name: 'Reply', exact: true }).click();
	await field.fill('Thank you for explaining');
	const response = page.waitForResponse((r) => r.url().includes(`/api/artifacts/${doc.artifactId}/comments`) && r.request().method() === 'POST');
	await card.getByRole('button', { name: 'Send reply' }).click();
	expect((await response).ok()).toBeTruthy();
	await expect(card).toContainText('Thank you for explaining');
	await expect(field).toBeVisible();
	await checkPane();
	if (width === 393) await page.getByRole('button', { name: 'Close comments' }).click();
	else await page.keyboard.press('Escape');
	await frame('sent');
	if (width === 393) await page.locator('main').getByRole('button', { name: 'Activity', exact: true }).click();
	await checkRow();
	await page.locator('[data-activity-row]').filter({ hasText: 'Another item 00' }).click();
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
			await page.locator('main').getByRole('button', { name: 'Activity', exact: true }).click();
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
