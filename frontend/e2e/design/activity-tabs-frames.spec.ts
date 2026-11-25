import fs from 'node:fs';
import path from 'node:path';
import { test, expect } from '../support/test';
import { createSession, openAppPastLanding, TEST_TOKEN } from '../support/corsia';
import { comment, seed } from '../support/reviewFixtures';
import { raiseAsk } from '../support/reviewAsk';
import { isolatedDb } from '../support/imageRegionComments';

const out = process.env.ACTIVITY_TABS_OUT;
test.use({ seedPerTest: true, colorScheme: 'light' });

for (const width of [1440, 393]) {
 test(`Activity tabs frame capture at ${width}`, async ({ page, request }, info) => {
  test.skip(info.project.name !== 'chromium-desktop');
  test.setTimeout(150_000);
  if (!out) throw new Error('Set ACTIVITY_TABS_OUT to a disposable evidence directory');
  fs.mkdirSync(out, { recursive: true });
  const doc = await seed(request, `Activity frame ${width}`);
  doc.text = '# Rollout plan\n\n' + Array.from({ length: 12 }, (_, i) =>
   `Step ${i + 1}: the rollout proceeds to the next ring.\n\n` + 'The team checks the result before proceeding. '.repeat(12) + '\n\n').join('');
  const updated = await request.put('/api/files/content', {
   params: { token: TEST_TOKEN }, data: { path: doc.docPath, text: doc.text, thread_id: doc.threadId },
  });
  expect(updated.ok()).toBeTruthy();
  const rootId = await comment(request, doc, 'Why does this step work?', 10);
  const response = await request.post(`/api/artifacts/${doc.artifactId}/comments`, {
   params: { token: TEST_TOKEN }, data: { body: 'Here is the explanation.', parent_id: rootId, delivery_policy: 'immediate' },
  });
  expect(response.ok()).toBeTruthy();
  const replyId = (await response.json()).comment.id as string;
  isolatedDb([
   'changed = connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Agent", sys.argv[2], sys.argv[3])).rowcount',
   'assert changed == 1, changed',
  ], doc.threadId, replyId);
  const review = await seed(request, `Review frame ${width}`);
  raiseAsk(review.threadId, review.artifactId, 'Review this document');
  const queueResponse = await request.get('/api/attention/queue', { params: { token: TEST_TOKEN, view: 'activity' } });
  expect(queueResponse.ok()).toBeTruthy();
  const base = await queueResponse.json();
  const reviewRow = base.items.find((item: { kind: string }) => item.kind === 'review');
  const docRow = base.notifications.items.find((item: { key: string }) => item.key === `doc:${doc.artifactId}`);
  expect(reviewRow).toBeTruthy();
  expect(docRow).toBeTruthy();
  const tasks = [{ ...reviewRow, rank: { position: 1, reason: 'First in order', ranked_by: doc.threadId, ranked_at: new Date().toISOString() } }, ...Array.from({ length: 99 }, (_, i) => ({
   ...reviewRow, key: `task:frame-${i}`, external_id: `frame-${i}`, renderer: 'task', kind: 'task',
   title: `Task ${i + 2}: Review the next item`, open: null,
   rank: { position: i + 2, reason: 'Next in order', ranked_by: doc.threadId, ranked_at: new Date().toISOString() },
   detail: { plan_id: 'frame-plan', node_id: `frame-${i}`, revision: 1, description: 'Review this item.' },
  }))];
  const notes = [docRow, ...Array.from({ length: 199 }, (_, i) => ({
   ...docRow, key: `doc:frame-${i}`, external_id: `doc:frame-${i}`, title: `Document ${i + 2}.md`,
   open: { artifact_id: `frame-${i}`, comment_id: 'frame-root' },
   detail: { ...docRow.detail, artifact_id: `frame-${i}` },
  }))];
  let mode: 'full' | 'empty' = 'full';
  await page.route('**/api/attention/queue**', route => {
   if (!route.request().url().includes('view=activity')) return route.fallback();
   return route.fulfill({ json: { ...base, items: mode === 'full' ? tasks : [], count: mode === 'full' ? tasks.length : 0,
    notifications: { items: mode === 'full' ? notes : [], count: mode === 'full' ? notes.length : 0 } } });
  });
  const shot = async (name: string) => {
   await page.evaluate(() => document.fonts.ready);
   await page.screenshot({ path: path.join(out, `${name}-${width}-light.png`), animations: 'disabled' });
  };
  await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
  await openAppPastLanding(page);
  if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
  await expect(page.getByRole('tab', { name: /Your tasks.*100/ })).toBeVisible();
  await expect(page.locator('.pane-col .spinner')).toHaveCount(0);
  const tasksTab = page.getByRole('tab', { name: /Your tasks/ });
  const tabY = (await tasksTab.boundingBox())!.y;
  if (width === 393) {
   for (const tab of await page.getByRole('tab').all()) expect((await tab.boundingBox())!.height).toBeGreaterThanOrEqual(44);
   const probe = async (button: ReturnType<typeof page.locator>, name: string) => {
    const result = await button.evaluate((el) => {
     const box = el.getBoundingClientRect();
     return [-20, 0, 20].map((dy) => {
      const y = box.y + box.height / 2 + dy;
      return { y, hit: el.contains(document.elementFromPoint(box.x + box.width / 2, y)) };
     });
    });
    console.log(`${name} hit targets`, JSON.stringify(result));
    expect(result.map(({ hit }) => hit)).toEqual([true, true, true]);
   };
   await probe(page.locator('.activity .by .link'), 'Ranker');
   const taskRowY = (await page.locator('[data-activity-row]').first().boundingBox())!.y;
   await page.getByRole('tab', { name: /Notifications.*200/ }).click();
   await probe(page.getByRole('button', { name: 'Mark all read' }), 'Mark all read');
   const notificationRowY = (await page.locator('[data-activity-row]').first().boundingBox())!.y;
   console.log('First row y', JSON.stringify({ taskRowY, notificationRowY }));
   expect(notificationRowY).toBe(taskRowY);
   await tasksTab.click();
  }
  await shot('tasks');
  if (width === 1440) {
   await page.getByRole('tab', { name: /Your tasks/ }).focus();
   await page.keyboard.press('ArrowRight');
   await page.keyboard.press('ArrowLeft');
   await expect(page.getByRole('tab', { name: /Your tasks/ })).toBeFocused();
   expect(await page.getByRole('tab', { name: /Your tasks/ }).evaluate((el) => el.matches(':focus-visible'))).toBe(true);
   await shot('tab-focus');
  }
  await page.locator('[data-activity-row]').last().scrollIntoViewIfNeeded();
  await expect(page.locator('[data-activity-row]').last()).toContainText('100');
  await expect(page.getByRole('tab', { name: /Your tasks/ })).toBeInViewport();
  await expect(page.getByRole('tab', { name: /Notifications/ })).toBeInViewport();
  expect(Math.abs((await tasksTab.boundingBox())!.y - tabY)).toBeLessThanOrEqual(1);
  await shot('rank-100');
  await page.getByRole('tab', { name: /Notifications.*200/ }).click();
  const markAll = page.getByRole('button', { name: 'Mark all read' });
  await expect(markAll).toBeVisible();
  await shot('notifications');
  await page.route('**/api/attention/notifications/read', route => route.fulfill({ status: 500, json: {} }));
  await page.getByRole('button', { name: 'Mark all read' }).click();
  await expect(page.getByRole('alert')).toContainText('Couldn’t mark these read. Try again.');
  await shot('mark-all-failed');
  await page.unroute('**/api/attention/notifications/read');
  await page.reload();
  if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
  await page.getByRole('tab', { name: /Notifications/ }).click();
  if (width === 1440) {
   await page.locator('[data-activity-row]').first().hover();
   await shot('dismiss-hover');
  }
  await page.locator('[data-activity-row]').filter({ hasText: doc.name }).click();
  const bar = page.getByRole('region', { name: 'Replies to your comments' });
  await expect(bar).toBeVisible();
  await expect(page.locator('.pane-col .cm-content')).toContainText('Step 10:');
  const card = page.getByRole('region', { name: /^Comment by/ }).filter({ hasText: 'Why does this step work?' });
  await expect(card).toBeVisible();
  await expect(card).toContainText('Here is the explanation.');
  await expect(page.locator('.pane-col .cm-comment-anchor').first()).toBeInViewport();
  const barBox = (await bar.boundingBox())!;
  const editorBox = (await page.locator('.pane-col .cm-editor').boundingBox())!;
  expect(barBox.y + barBox.height).toBeLessThanOrEqual(editorBox.y);
  const anchorBox = (await page.locator('.pane-col .cm-comment-anchor').first().boundingBox())!;
  const cardBox = (await card.boundingBox())!;
  expect(anchorBox.y).toBeGreaterThanOrEqual(editorBox.y + 12);
  expect(anchorBox.y + anchorBox.height <= cardBox.y || cardBox.y + cardBox.height <= anchorBox.y || anchorBox.x + anchorBox.width <= cardBox.x || cardBox.x + cardBox.width <= anchorBox.x).toBeTruthy();
  if (width === 393) expect((await bar.getByRole('button', { name: 'Done', exact: true }).boundingBox())!.height).toBeGreaterThanOrEqual(44);
  await shot('document-open');
  console.log('landing geometry', width, JSON.stringify({ bar: await bar.boundingBox(), anchor: await page.locator('.pane-col .cm-comment-anchor').first().boundingBox(), card: await card.boundingBox(), editor: await page.locator('.pane-col .cm-editor').boundingBox() }));
  if (width === 393 && await page.getByRole('dialog', { name: 'Document comments' }).isVisible()) {
   await page.getByRole('dialog', { name: 'Document comments' }).getByRole('button', { name: 'Close comments' }).click();
   await expect(page.getByRole('dialog', { name: 'Document comments' })).toBeHidden();
  }
  await page.route('**/api/attention/notifications/read', route => route.fulfill({ status: 500, json: {} }));
  await bar.getByRole('button', { name: 'Done', exact: true }).click();
  await expect(bar.getByRole('alert')).toContainText('Couldn’t mark these replies read. Try again.');
  await expect.poll(async () => {
   const anchor = await page.locator('.pane-col .cm-comment-anchor').first().boundingBox();
   const editor = await page.locator('.pane-col .cm-editor').boundingBox();
   return anchor && editor ? anchor.y - editor.y : -1;
  }).toBeGreaterThanOrEqual(12);
  console.log('failed geometry', width, JSON.stringify({ bar: await bar.boundingBox(), anchor: await page.locator('.pane-col .cm-comment-anchor').first().boundingBox(), editor: await page.locator('.pane-col .cm-editor').boundingBox() }));
  await shot('done-failed');
  mode = 'empty';
  await page.reload();
  if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
  await expect(page.getByText('Nothing needs you.').first()).toBeVisible();
  await shot('tasks-empty');
  await page.getByRole('tab', { name: /Notifications/ }).click();
  await expect(page.getByText('No unread replies.').first()).toBeVisible();
  await shot('notifications-empty');
 });
}

test('five replies from three agents, dark desktop', async ({ page, request }, info) => {
 test.skip(info.project.name !== 'chromium-desktop');
 test.setTimeout(150_000);
 if (!out) throw new Error('Set ACTIVITY_TABS_OUT to a disposable evidence directory');
 fs.mkdirSync(out, { recursive: true });
 const doc = await seed(request, 'Multiple replies');
 const agentB = await createSession(request, 'Second agent');
 const agentC = await createSession(request, 'Third agent');
 const agents = [doc.threadId, agentB, agentC, agentB, doc.threadId];
 for (let i = 0; i < 5; i++) {
  const rootId = await comment(request, doc, `Question ${i + 1}`, i + 5);
  const response = await request.post(`/api/artifacts/${doc.artifactId}/comments`, {
   params: { token: TEST_TOKEN }, data: { body: `Answer ${i + 1}`, parent_id: rootId, delivery_policy: 'immediate' },
  });
  expect(response.ok()).toBeTruthy();
  const replyId = (await response.json()).comment.id as string;
  isolatedDb([
   'changed = connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Agent", sys.argv[2], sys.argv[3])).rowcount',
   'assert changed == 1, changed',
  ], agents[i], replyId);
 }
 await page.addInitScript(() => localStorage.setItem('corsia_theme', 'dark'));
 await page.setViewportSize({ width: 1440, height: 900 });
 await openAppPastLanding(page);
 await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
 await page.getByRole('tab', { name: /Notifications/ }).click();
 const firstNotification = page.locator('[data-activity-row]').filter({ hasText: doc.name });
 await expect(firstNotification).toContainText('5 unread replies');
 await expect(firstNotification).toHaveAttribute('aria-current', 'true');
 const bar = page.getByRole('region', { name: 'Replies to your comments' });
 await expect(bar).toContainText('and 2 others');
 await expect(bar).toContainText('1 of 5 replies to your comments');
 const card = page.getByRole('region', { name: /^Comment by/ }).filter({ hasText: 'Question 5' });
 await expect(card).toContainText('Answer 5');
 const popover = card.locator('xpath=ancestor::*[@data-popover]');
 await expect(popover).toHaveCSS('opacity', '1');
 await expect(popover).toHaveCSS('background-color', 'rgb(28, 28, 31)');
 await page.screenshot({ path: path.join(out, 'notifications-1440-dark.png') });
});

for (const width of [1440, 393] as const) {
 test(`paired document frame ${width}`, async ({ page, request }, info) => {
  test.skip(info.project.name !== 'chromium-desktop');
  test.setTimeout(150_000);
  if (!out) throw new Error('Set ACTIVITY_TABS_OUT to a disposable evidence directory');
  fs.mkdirSync(out, { recursive: true });
  const doc = await seed(request, 'Multiple replies');
  doc.text = '# Rollout plan\n\n' + Array.from({ length: 12 }, (_, i) =>
   `Step ${i + 1}: the rollout proceeds to the next ring.\n\n` + 'The team checks the result before proceeding. '.repeat(12) + '\n\n').join('');
  expect((await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: doc.docPath, text: doc.text, thread_id: doc.threadId } })).ok()).toBeTruthy();
  const agents = [doc.threadId, await createSession(request, 'Second agent'), await createSession(request, 'Third agent')];
  for (let i = 0; i < 5; i++) {
   const rootId = await comment(request, doc, `Question ${i + 1}`, i + 5);
   const response = await request.post(`/api/artifacts/${doc.artifactId}/comments`, {
    params: { token: TEST_TOKEN }, data: { body: `Answer ${i + 1}`, parent_id: rootId, delivery_policy: 'immediate' },
   });
   expect(response.ok()).toBeTruthy();
   const replyId = (await response.json()).comment.id as string;
   isolatedDb([
    'changed = connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Agent", sys.argv[2], sys.argv[3])).rowcount',
    'assert changed == 1, changed',
   ], agents[i % agents.length], replyId);
  }
  const queue = await request.get('/api/attention/queue', { params: { token: TEST_TOKEN, view: 'activity' } });
  expect(queue.ok()).toBeTruthy();
  const base = await queue.json();
  const real = base.notifications.items.find((item: { key: string }) => item.key === `doc:${doc.artifactId}`);
  expect(real).toBeTruthy();
  const notes = [real, ...Array.from({ length: 199 }, (_, i) => ({
   ...real, key: `doc:pair-${i}`, external_id: `doc:pair-${i}`, title: `Document ${i + 2}.md`,
   open: { artifact_id: `pair-${i}`, comment_id: 'pair-root' }, detail: { ...real.detail, artifact_id: `pair-${i}` },
  }))];
  await page.route('**/api/attention/queue**', route => route.request().url().includes('view=activity')
   ? route.fulfill({ json: { ...base, notifications: { items: notes, count: notes.length } } }) : route.fallback());
  await page.addInitScript(() => localStorage.setItem('corsia_theme', 'light'));
  await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
  await openAppPastLanding(page);
  if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
  await page.getByRole('tab', { name: /Notifications.*200/ }).click();
  await page.locator('[data-activity-row]').filter({ hasText: doc.name }).click();
  const bar = page.getByRole('region', { name: 'Replies to your comments' });
  await expect(bar).toContainText('and 2 others');
  await expect(bar).toContainText('1 of 5 replies to your comments');
  await expect(page.getByRole('button', { name: width === 393 ? /5 unread/ : /5 new/ })).toBeVisible();
  await expect(page.getByRole('region', { name: /^Comment by/ }).filter({ hasText: 'Question 5' })).toContainText('Answer 5');
  if (width === 393) await expect(page.locator('.h-back-label')).toHaveText('Notifications');
  await page.mouse.move(0, 0);
  await page.screenshot({ path: path.join(out, width === 1440 ? 'pair-notes-1440.png' : 'pair-doc-393.png'), animations: 'disabled' });
  // The phone sheet is modal; close it before using the document's Done control.
  if (width === 393) {
   await page.getByRole('button', { name: 'Close comments' }).click();
   await expect(page.getByRole('dialog', { name: 'Document comments' })).toBeHidden();
  }
  await page.route('**/api/attention/notifications/read', route => route.fulfill({ status: 500, json: {} }));
  await bar.getByRole('button', { name: 'Done', exact: true }).click();
  await expect(bar.getByRole('alert')).toContainText('Couldn’t mark these replies read. Try again.');
  await expect(bar.getByRole('button', { name: 'Try again' })).toBeVisible();
  await expect(page.getByText('Couldn’t mark these read. Try again.', { exact: true })).toHaveCount(0);
  await expect(page.locator('.cm-comment-focused').first()).toBeVisible();
  await page.screenshot({ path: path.join(out, `pair-done-failed-${width}.png`), animations: 'disabled' });
  await page.locator('.cm-comment-focused').first().click();
  await expect(page.getByRole('region', { name: /^Comment by/ }).filter({ hasText: 'Question 5' })).toBeVisible();
 });
}
