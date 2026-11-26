import path from 'node:path';
import { test, expect, type Page } from '../support/test';
import { createSession, openAppPastLanding, TEST_TOKEN } from '../support/corsia';
import { injectMessage } from '../support/mock';
import { isolatedDb } from '../support/imageRegionComments';

test.use({ seedPerTest: true });

async function capture(page: Page, path: string, keepHover = false) {
  if (!keepHover) await page.mouse.move(0, 0);
  await page.evaluate(async () => {
    (document.activeElement as HTMLElement | null)?.blur();
    await document.fonts.ready;
    await Promise.all(document.getAnimations().filter(a => a.effect?.getComputedTiming().iterations !== Infinity).map(a => a.finished.catch(() => {})));
  });
  await page.screenshot({ path, animations: 'disabled' });
}

for (const [width, action] of [[1440, 'done'], [393, 'done'], [1440, 'jump'], [393, 'jump'], [1440, 'all'], [393, 'all'], [1440, 'selected-all'], [393, 'selected-all'], [1440, 'overflow'], [393, 'overflow'], [1440, 'close'], [393, 'close']] as const) {
  test(`Activity shows document replies and chat replies at ${width}: ${action}`, async ({ page, request }, info) => {
    test.skip(process.env.CORSIA_E2E_MOCK !== '1' || info.project.name !== 'chromium-desktop');
    test.setTimeout(120_000);
    const thread = await createSession(request, `Reply agent ${width}`);
    const owner = await createSession(request, `Document owner ${width}`);
    const target = await injectMessage(request, thread, { role: 'assistant', content: 'Message being commented on.', metadata: { for_user: true } }, TEST_TOKEN);
    const answer = await injectMessage(request, thread, { role: 'assistant', content: '[c1.1] Chat answer.', metadata: { for_user: true } }, TEST_TOKEN);
    const secondAnswer = action === 'jump' ? await injectMessage(request, thread, { role: 'assistant', content: '[c1.1] Another answer.', metadata: { for_user: true } }, TEST_TOKEN) : null;
    const overflowTarget = action === 'overflow' ? await injectMessage(request, thread, { role: 'assistant', content: `Second message. ${'A longer discussion for context. '.repeat(45)}`, metadata: { for_user: true } }, TEST_TOKEN) : null;
    const overflowAnswers: string[] = [];
    if (overflowTarget) for (let i = 1; i <= 3; i++) overflowAnswers.push(await injectMessage(request, thread, { role: 'assistant', content: `[c2.1] Additional answer ${i}.`, metadata: { for_user: true } }, TEST_TOKEN));
    isolatedDb([
      'thread, target, answer = sys.argv[2:5]',
      'from datetime import datetime, timedelta, timezone',
      'now = datetime.now(timezone.utc)',
      'comment_at = now.isoformat()',
      'answer_at = (now + timedelta(seconds=1)).isoformat()',
      'meta = json.dumps({"reply_context": {"comments": [{"id": "comment-1", "cid": "c1.1", "seq": 1, "note": "Chat question", "quote": "Message", "from_off": 1, "to_off": 8, "anchor_prefix": " ", "anchor_suffix": " being commented on."}]}})',
      'connection.execute("INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq) VALUES (?,?,?,?,?,?,?)", ("user-comment",thread,"user","Please answer my comment.",meta,comment_at,99))',
      'connection.execute("INSERT INTO chat_comments(thread_id,target_message_id,user_message_id,cid,created_at) VALUES (?,?,?,?,?)", (thread,target,"user-comment","c1.1",comment_at))',
      'connection.execute("UPDATE messages SET created_at=? WHERE id=?", (answer_at,answer))',
      'connection.execute("INSERT INTO chat_visible_content(message_id,thread_id,content) VALUES (?,?,?)", (answer,thread,"Chat answer."))',
      'connection.execute("INSERT INTO chat_comment_answers(thread_id,user_message_id,cid,assistant_message_id,answered_at) VALUES (?,?,?,?,?)", (thread,"user-comment","c1.1",answer,answer_at))',
    ], thread, target, answer);
    if (secondAnswer) isolatedDb([
      'from datetime import datetime, timedelta, timezone',
      'stamp = (datetime.now(timezone.utc) + timedelta(seconds=2)).isoformat()',
      'connection.execute("UPDATE messages SET created_at=? WHERE id=?", (stamp,sys.argv[3]))',
      'connection.execute("INSERT INTO chat_visible_content(message_id,thread_id,content) VALUES (?,?,?)", (sys.argv[3],sys.argv[2],"Another answer."))',
      'connection.execute("INSERT INTO chat_comment_answers(thread_id,user_message_id,cid,assistant_message_id,answered_at) VALUES (?,?,?,?,?)", (sys.argv[2],"user-comment","c1.1",sys.argv[3],stamp))',
    ], thread, secondAnswer);
    if (overflowTarget) {
      isolatedDb([
        'from datetime import datetime, timedelta, timezone',
        'thread, target, *answers = sys.argv[2:]',
        'now = datetime.now(timezone.utc)',
        'stamp = now.isoformat()',
        'meta = json.dumps({"reply_context": {"comments": [{"id": "comment-2", "cid": "c2.1", "seq": 3, "note": "Second question", "quote": "Second", "from_off": 1, "to_off": 7, "anchor_prefix": " ", "anchor_suffix": " message."}]}})',
        'connection.execute("INSERT INTO messages(id,thread_id,role,content,metadata,created_at,seq) VALUES (?,?,?,?,?,?,?)", ("user-comment-2",thread,"user","Please answer this too.",meta,stamp,100))',
        'connection.execute("INSERT INTO chat_comments(thread_id,target_message_id,user_message_id,cid,created_at) VALUES (?,?,?,?,?)", (thread,target,"user-comment-2","c2.1",stamp))',
        'for i, answer in enumerate(answers, 1):',
        '  at = (now + timedelta(seconds=i)).isoformat()',
        '  connection.execute("UPDATE messages SET created_at=? WHERE id=?", (at,answer))',
        '  connection.execute("INSERT INTO chat_visible_content(message_id,thread_id,content) VALUES (?,?,?)", (answer,thread,f"Additional answer {i}."))',
        '  connection.execute("INSERT INTO chat_comment_answers(thread_id,user_message_id,cid,assistant_message_id,answered_at) VALUES (?,?,?,?,?)", (thread,"user-comment-2","c2.1",answer,at))',
      ], thread, overflowTarget, ...overflowAnswers);
    }
    const tree = await (await request.get('/api/files/tree', { params: { token: TEST_TOKEN } })).json();
    const root = tree.roots.find((r: { kind: string }) => r.kind === 'workspace').path;
    const created = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: path.join(root, 'activity-reply.md'), text: '# activity-reply.md\n\nDocument context\n', thread_id: owner, create: true } });
    expect(created.ok()).toBeTruthy();
    const artifact = (await created.json()).artifact.id;
    const comment = await request.post(`/api/artifacts/${artifact}/comments`, { params: { token: TEST_TOKEN }, data: { body: 'Document question', anchor_text: 'Document', from_off: 21, to_off: 29, delivery_policy: 'immediate' } });
    expect(comment.ok()).toBeTruthy();
    const rootId = (await comment.json()).comment.id;
    const reply = await request.post(`/api/artifacts/${artifact}/comments`, { params: { token: TEST_TOKEN }, data: { body: 'Document answer', parent_id: rootId, delivery_policy: 'immediate' } });
    expect(reply.ok()).toBeTruthy();
    const replyId = (await reply.json()).comment.id;
    isolatedDb(['connection.execute("UPDATE doc_comments SET author_kind=\'agent\', author_thread_id=? WHERE id=?", (sys.argv[2],sys.argv[3]))'], thread, replyId);
    const response = await request.get('/api/attention/queue', { params: { token: TEST_TOKEN, view: 'activity' } });
    expect(response.ok()).toBeTruthy();
    const queue = await response.json();
    const chatRows = queue.notifications.items.filter((item: { key: string }) => item.key === `chat-reply:${thread}`);
    expect(chatRows).toHaveLength(1);
    expect(chatRows[0].detail.unread).toBe(action === 'overflow' ? 4 : action === 'jump' ? 2 : 1);
    expect(queue.notifications.items.some((item: { detail: { artifact_id: string } }) => item.detail.artifact_id === artifact)).toBe(true);
    await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
    await openAppPastLanding(page);
    if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
    await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
    await page.getByRole('tab', { name: /Notifications/ }).click();
    await expect(page.locator('[data-activity-row]').filter({ hasText: 'activity-reply.md' })).toBeVisible();
    const chatRow = page.locator('[data-activity-row]').filter({ hasText: `Reply agent ${width}` });
    await expect(chatRow).toHaveCount(1);
    await expect(chatRow).toContainText(action === 'overflow' ? 'Chat · 4 unread replies ·' : action === 'jump' ? 'Chat · 2 unread replies ·' : 'Chat · 1 unread reply ·');
    await capture(page, info.outputPath('mixed.png'));
    const dismiss = chatRow.locator('..').locator('.dismiss');
    await expect(dismiss).toHaveCSS(width === 393 ? 'display' : 'opacity', width === 393 ? 'none' : '0');
    if (width === 1440 && action === 'done') {
      await page.locator('[data-activity-row]').filter({ hasText: 'activity-reply.md' }).click();
      await expect(chatRow).not.toHaveAttribute('aria-current', 'true');
      await capture(page, info.outputPath('mixed-document-selected.png'));
      await chatRow.hover();
      await expect(dismiss).toHaveCSS('opacity', '1');
      await capture(page, info.outputPath('mixed-chat-hover.png'), true);
      await page.mouse.move(0, 0);
    }
    if (action === 'all') {
      await page.locator('[data-activity-row]').filter({ hasText: 'activity-reply.md' }).click();
      if (width === 393) await page.getByRole('button', { name: /Notifications/ }).click();
      await page.getByRole('button', { name: 'Mark all read' }).click();
      await expect(chatRow).toHaveCount(0);
      await expect(page.locator('[data-activity-row]').filter({ hasText: 'activity-reply.md' })).toContainText('All replies read');
      const after = await (await request.get('/api/attention/queue', { params: { token: TEST_TOKEN, view: 'activity' } })).json();
      expect(after.notifications.count).toBe(0);
      return;
    }
    await chatRow.click();
    await expect(page.getByText('Chat question', { exact: false })).toBeVisible();
    await expect(page.getByText('Chat answer', { exact: false })).toBeVisible();
    await expect(page.locator('.message .cm-comment-wrap').first()).toHaveText('Message');
    if (action === 'overflow') await expect(page.locator('.message .cm-comment-wrap').nth(1)).toHaveText('Second');
    if (action === 'jump') await expect(page.getByText('Another answer.', { exact: false })).toBeVisible();
    await expect(page.getByRole('button', { name: `Close Reply agent ${width}` })).toBeVisible();
    if (action === 'overflow') await expect(page.locator('[data-doc-jump]')).toContainText('3 new');
    await capture(page, info.outputPath('chat-open.png'));
    if (width === 1440) await expect(dismiss).toHaveCSS('opacity', '0');
    if (action === 'overflow') {
      await page.locator('[data-unread-answer]').nth(1).evaluate(el => el.scrollIntoView({ block: 'start' }));
      await expect(page.getByLabel('Replies to your comments')).toContainText('2 of 4 replies to your comments');
      await capture(page, info.outputPath('chat-second-reply.png'));
      await page.locator('.chat-pane .scroll').evaluate(el => { el.scrollTop = el.scrollHeight; });
      const lastReply = page.locator('.chat-pane .thread .text').last();
      const pill = page.locator('.chat-pane [data-doc-jump]');
      await expect(pill).toBeVisible();
      await capture(page, info.outputPath('chat-bottom.png'));
      await expect(lastReply).toBeInViewport();
      return;
    }
    if (action === 'close') {
      const refresh = page.waitForResponse(response => new URL(response.url()).pathname === '/api/attention/queue' && response.ok());
      await page.getByRole('button', { name: `Close Reply agent ${width}` }).click();
      await refresh;
      await expect(page.getByText('Chat question', { exact: false })).toHaveCount(0);
      await expect(chatRow).toBeVisible();
      await chatRow.click();
      await expect(page.getByText('Chat question', { exact: false })).toBeVisible();
      return;
    }
    if (action === 'selected-all') {
      if (width === 393) await page.getByRole('button', { name: /Notifications/ }).click();
      await page.getByRole('button', { name: 'Mark all read' }).click();
      await expect(chatRow).toContainText('All replies read');
      if (width === 393) await chatRow.click();
      await expect(page.getByRole('button', { name: 'Done', exact: true })).toHaveCount(0);
      await expect(page.getByText('All replies read.', { exact: true })).toBeVisible();
      return;
    }
    if (action === 'jump') {
      await page.getByRole('button', { name: /Go to message/ }).click();
      const message = page.locator(`[data-message-id="${target}"]`).first();
      await expect(message).toBeVisible();
      await expect(message.locator('[data-comment-ids][role="button"]')).toBeVisible();
      await expect(page.getByText('Chat question', { exact: false })).toBeVisible();
      await expect.poll(async () => {
        const response = await request.get('/api/attention/queue', { params: { token: TEST_TOKEN, view: 'activity' } });
        const queue = await response.json();
        return queue.notifications.items.some((item: { key: string }) => item.key === `chat-reply:${thread}`);
      }).toBe(false);
      await expect(message.locator('[data-comment-ids][role="button"]')).toContainText('Message');
      await expect(page.getByText('This text can’t be highlighted in the message.')).toHaveCount(0);
      await capture(page, info.outputPath('landed.png'));
      return;
    }
    await page.getByRole('button', { name: 'Done', exact: true }).click();
    await expect(page.getByText('Document context', { exact: false }).first()).toBeVisible();
    await capture(page, info.outputPath('after-done.png'));
    if (width === 393) {
      await page.getByRole('button', { name: 'Close comments' }).click();
      await page.getByRole('button', { name: /Notifications/ }).click();
    }
    await expect(chatRow).toHaveCount(0);
    const after = await (await request.get('/api/attention/queue', { params: { token: TEST_TOKEN, view: 'activity' } })).json();
    expect(after.notifications.items.some((item: { key: string }) => item.key === `chat-reply:${thread}`)).toBe(false);
    await page.locator('[data-activity-row]').filter({ hasText: 'activity-reply.md' }).click();
    await expect(page.getByText('Document context', { exact: false }).first()).toBeVisible();
  });
}
