import { test, expect } from '../support/test';
import { openAppPastLanding, TEST_TOKEN } from '../support/corsia';
import { comment, seed } from '../support/reviewFixtures';
import { isolatedDb } from '../support/imageRegionComments';

test.use({ hasTouch: true, viewport: { width: 393, height: 852 } });

test('a phone swipe marks the selected document read', async ({ page, request }) => {
	const doc = await seed(request, 'Swipe document');
	const rootId = await comment(request, doc, 'A question', 5);
	const reply = await request.post(`/api/artifacts/${doc.artifactId}/comments`, {
		params: { token: TEST_TOKEN }, data: { body: 'An answer', parent_id: rootId, delivery_policy: 'immediate' },
	});
	expect(reply.ok()).toBeTruthy();
	const replyId = (await reply.json()).comment.id as string;
	isolatedDb([
		'changed = connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Agent", sys.argv[2], sys.argv[3])).rowcount',
		'assert changed == 1, changed',
	], doc.threadId, replyId);
	await openAppPastLanding(page);
	await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
	await page.getByRole('tab', { name: /Notifications/ }).click();
	const row = page.locator('[data-activity-row]').filter({ hasText: doc.name });
	await expect(row).toBeVisible();
	const box = (await row.boundingBox())!;
	const client = await page.context().newCDPSession(page);
	const y = box.y + box.height / 2;
	const start = box.x + box.width - 10;
	const end = box.x + 10;
	const read = page.waitForResponse((response) => response.url().includes('/api/attention/notifications/read') && response.request().method() === 'POST');
	await client.send('Input.dispatchTouchEvent', { type: 'touchStart', touchPoints: [{ x: start, y }] });
	await client.send('Input.dispatchTouchEvent', { type: 'touchMove', touchPoints: [{ x: end, y }] });
	await client.send('Input.dispatchTouchEvent', { type: 'touchEnd', touchPoints: [] });
	expect((await read).ok()).toBeTruthy();
	await expect(row).toHaveCount(0);
	await expect(page.getByText('No unread replies.').first()).toBeVisible();
});
