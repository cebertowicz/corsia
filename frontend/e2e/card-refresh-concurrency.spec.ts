import fs from 'node:fs';
import path from 'node:path';
import { expect, type WebSocketRoute } from '@playwright/test';
import { test } from './support/test';
import { e2eModel, openAppPastLanding, sidebarThread, TEST_TOKEN } from './support/corsia';
import { injectMessage } from './support/mock';

test('a change during a card refresh is reflected by a trailing read', async ({ page, request }) => {
  const workspace = process.env.CORSIA_E2E_WORKSPACE!;
  const name = `card-concurrency-${Date.now()}.md`;
  const file = path.join(workspace, name);
  fs.writeFileSync(file, '# card\n');
  const title = `Card concurrency ${Date.now()}`;
  const created = await request.post('/api/threads', { params: { token: TEST_TOKEN }, data: { title, model: e2eModel() } });
  expect(created.ok()).toBeTruthy();
  const thread = (await created.json()).thread_id as string;
  await injectMessage(request, thread, { role: 'assistant', content: `File ${name}`, metadata: { for_user: true } }, TEST_TOKEN);
  let socket: WebSocketRoute | null = null;
  await page.routeWebSocket(url => url.pathname === '/ws', client => {
    const server = client.connectToServer();
    socket = client;
    client.onMessage(data => server.send(data));
    server.onMessage(data => client.send(data));
  });
  await openAppPastLanding(page);
  await sidebarThread(page, title).click();
  await expect(page.locator('[data-message-id]')).toHaveCount(1);
  await expect.poll(() => socket !== null).toBe(true);
  let release!: () => void;
  const held = new Promise<void>(resolve => { release = resolve; });
  let captured = false;
  let reads = 0;
  await page.route(`**/api/threads/${thread}/messages/refresh**`, async route => {
    reads++;
    const response = await route.fetch();
    if (reads === 1) { captured = true; await held; }
    await route.fulfill({ response });
  });
  try {
    (socket as unknown as WebSocketRoute).send(JSON.stringify({ type: 'artifacts_changed', thread_id: thread }));
    await expect.poll(() => captured).toBe(true);
    const registered = await request.post('/api/files/register', { params: { token: TEST_TOKEN }, data: { path: file, thread_id: thread } });
    expect(registered.ok(), await registered.text()).toBeTruthy();
    const artifact = (await registered.json()).artifact.id as string;
    (socket as unknown as WebSocketRoute).send(JSON.stringify({ type: 'artifacts_changed', thread_id: thread, artifact_id: artifact }));
    await page.waitForTimeout(500);
    expect(reads).toBe(1);
    release();
    await expect.poll(() => reads).toBe(2);
    await expect(page.locator(`[data-artifact-id="${artifact}"]`).first()).toBeVisible();
  } finally {
    release();
  }
});
