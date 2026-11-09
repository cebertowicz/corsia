import fs from 'node:fs';
import path from 'node:path';
import { expect, type Page, type APIRequestContext } from '@playwright/test';
import { createSession, loginByUrl, sidebarThread, TEST_TOKEN } from '../support/corsia';
import { syntheticPng } from '../support/syntheticImages';
import { isolatedDb } from '../support/heldComments';

const image = '![Review screen](shots/screen.png)';
const tableCell = 'The navigation remains visible while the document scrolls through a lengthy review with comments from several contributors.';
const doc = [
  '# Design review walkthrough', '',
  'This document collects the outstanding decisions for the responsive review.', '',
  image, '',
  '## Review checklist', '',
  '- Check whether the controls remain visible at narrow widths.',
  '- Read the comments alongside the document.',
  '- Confirm that the page remains legible in both themes.', '',
  '```ts', 'const review = { status: "ready", themes: ["light", "dark"] };', '```', '',
  '| Area | Review note | Preview |', '| --- | --- | --- |',
  `| Navigation | ${tableCell} | ![Small preview](shots/screen.png) |`, '',
  ...Array.from({ length: 24 }, (_, i) => `## Review section ${i + 1}\n\nThe team can compare the layout at each width and return to the original comment without losing its context. The review should describe what changed and what remains to be decided.\n`),
].join('\n');

export const scrollAnchor = '.cm-content';

export async function seed(page: Page, request: APIRequestContext) {
  const title = `Responsive Design Review and Accessibility Coordination Agent ${Date.now()}`;
  const thread = await createSession(request, title);
  const params = { token: TEST_TOKEN };
  const roots = (await (await request.get('/api/files/tree', { params })).json()).roots as Array<{ kind: string; path: string }>;
  const workspace = roots.find(root => root.kind === 'workspace')?.path;
  if (!workspace) throw new Error('Disposable workspace was not returned by the mock backend');
  const directory = path.join(workspace, `contact-sheet-document-${thread}`);
  fs.mkdirSync(path.join(directory, 'shots'), { recursive: true });
  fs.writeFileSync(path.join(directory, 'shots', 'screen.png'), syntheticPng(800, 500));
  const docPath = path.join(directory, 'review.md');
  fs.writeFileSync(docPath, doc);
  const registered = await request.post('/api/files/register', { params, data: { path: docPath, thread_id: thread } });
  expect(registered.ok(), await registered.text()).toBeTruthy();
  const artifact = (await registered.json()).artifact.id as string;
  async function comment(anchor: string, body: string, region?: { x: number; y: number; w: number; h: number }) {
    const from = doc.indexOf(anchor);
    const response = await request.post(`/api/artifacts/${artifact}/comments`, { params, data: { body, anchor_text: anchor, from_off: from, to_off: from + anchor.length, region, delivery_policy: 'immediate' } });
    expect(response.ok(), await response.text()).toBeTruthy();
    return (await response.json()).comment.id as string;
  }
  const agentComment = await comment('outstanding decisions', 'Which decisions need a separate review?');
  isolatedDb(['connection.execute("UPDATE doc_comments SET author=?, author_kind=?, author_thread_id=? WHERE id=?", (sys.argv[2], "agent", sys.argv[3], sys.argv[4]))'], title, thread, agentComment);
  await comment(image, 'The title should remain legible on small screens.', { x: 0.15, y: 0.15, w: 0.4, h: 0.2 });
  const resolved = await comment(tableCell, 'This explanation now includes the expected behavior.');
  const patch = await request.patch(`/api/comments/${resolved}`, { params, data: { resolved: true } });
  expect(patch.ok(), await patch.text()).toBeTruthy();
  async function open(target: Page) {
    await target.addInitScript(({ thread, artifact, docPath }) => {
      localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [thread]: { tabs: [{ instanceId: 'contact-sheet', artifactId: artifact, path: docPath, title: 'review.md', loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'contact-sheet', visible: true, activeSurface: 'artifact' } }, splitWidth: null }));
    }, { thread, artifact, docPath });
    await loginByUrl(target, { waitForComposer: false });
    if ((target.viewportSize()?.width ?? 0) <= 768) await target.getByRole('button', { name: 'Toggle sidebar' }).click();
    await sidebarThread(target, title).click();
    await expect(target.locator('.cm-content')).toBeVisible();
    const images = target.locator('.cm-md-image img, .cm-md-table td img');
    await expect(images).toHaveCount(2);
    for (let i = 0; i < 2; i++) await expect.poll(() => images.nth(i).evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0);
  }
  await open(page);
  return open;
}
