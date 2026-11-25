import path from 'node:path';
import { test, expect } from '../support/test';
import { createSession, openAppPastLanding, TEST_TOKEN } from '../support/corsia';
import { injectMessage } from '../support/mock';
import { isolatedDb } from '../support/imageRegionComments';

test.use({ seedPerTest: true });

for (const width of [1440, 393]) {
  test(`Activity keeps document replies and excludes chat replies at ${width}`, async ({ page, request }, info) => {
    test.skip(process.env.CORSIA_E2E_MOCK !== '1' || info.project.name !== 'chromium-desktop');
    test.setTimeout(120_000);
    const thread = await createSession(request, `Reply agent ${width}`);
    const owner = await createSession(request, `Document owner ${width}`);
    const target = await injectMessage(request, thread, { role: 'assistant', content: 'Message being commented on.', metadata: { for_user: true } }, TEST_TOKEN);
    const answer = await injectMessage(request, thread, { role: 'assistant', content: '[c1.1] Chat answer.', metadata: { for_user: true } }, TEST_TOKEN);
    isolatedDb([
      'thread, target, answer = sys.argv[2:5]',
      'meta = json.dumps({"reply_context": {"comments": [{"cid": "c1.1", "seq": 1, "note": "Chat question", "quote": "Message"}]}})',
      'connection.execute("INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq) VALUES (?,?,?,?,?,?,?)", ("user-comment",thread,"user","",meta,"2026-11-24T00:00:00Z",99))',
      'connection.execute("INSERT INTO chat_comments(thread_id,target_message_id,user_message_id,cid,created_at) VALUES (?,?,?,?,?)", (thread,target,"user-comment","c1.1","2026-11-24T00:00:00Z"))',
      'connection.execute("INSERT INTO chat_comment_answers(thread_id,user_message_id,cid,assistant_message_id,answered_at) VALUES (?,?,?,?,?)", (thread,"user-comment","c1.1",answer,"2026-11-24T00:01:00Z"))',
    ], thread, target, answer);
    const tree = await (await request.get('/api/files/tree', { params: { token: TEST_TOKEN } })).json();
    const root = tree.roots.find((r: { kind: string }) => r.kind === 'workspace').path;
    const created = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: path.join(root, 'activity-reply.md'), text: '# Document context\n', thread_id: owner, create: true } });
    expect(created.ok()).toBeTruthy();
    const artifact = (await created.json()).artifact.id;
    const comment = await request.post(`/api/artifacts/${artifact}/comments`, { params: { token: TEST_TOKEN }, data: { body: 'Document question', anchor_text: 'Document', from_off: 2, to_off: 10, delivery_policy: 'immediate' } });
    expect(comment.ok()).toBeTruthy();
    const rootId = (await comment.json()).comment.id;
    const reply = await request.post(`/api/artifacts/${artifact}/comments`, { params: { token: TEST_TOKEN }, data: { body: 'Document answer', parent_id: rootId, delivery_policy: 'immediate' } });
    expect(reply.ok()).toBeTruthy();
    const replyId = (await reply.json()).comment.id;
    isolatedDb(['connection.execute("UPDATE doc_comments SET author_kind=\'agent\', author_thread_id=? WHERE id=?", (sys.argv[2],sys.argv[3]))'], thread, replyId);
    const response = await request.get('/api/attention/queue', { params: { token: TEST_TOKEN, view: 'activity' } });
    expect(response.ok()).toBeTruthy();
    const items = (await response.json()).items;
    const before = process.env.ACTIVITY_BEFORE === '1';
    expect(items.some((item: { key: string }) => item.key.startsWith('chat-reply:'))).toBe(before);
    expect(items.some((item: { key: string }) => item.key.startsWith('doc-reply:'))).toBe(true);
    await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
    await openAppPastLanding(page);
    if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
    await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
    await expect(page.getByText('Document question', { exact: false }).first()).toBeVisible();
    if (before) await expect(page.getByText('Chat question', { exact: false }).first()).toBeVisible();
    else await expect(page.getByText('Chat question', { exact: false })).toHaveCount(0);
    if (process.env.ACTIVITY_OUT) await page.screenshot({ path: path.join(process.env.ACTIVITY_OUT, `${before ? 'before' : 'after'}-${width}.png`), animations: 'disabled' });
    const docRow = page.locator('[data-activity-row]').filter({ hasText: 'Document question' });
    await docRow.click();
    await page.waitForTimeout(1500);
    if (process.env.ACTIVITY_OUT) await page.screenshot({ path: path.join(process.env.ACTIVITY_OUT, `${before ? 'before' : 'after'}-document-${width}.png`), animations: 'disabled' });
    console.log('DOCUMENT_OPENED', width, await page.getByText('Document context', { exact: false }).count());
  });
}
