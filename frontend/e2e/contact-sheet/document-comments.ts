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

const longReply = [
  'I checked this on a phone, a small tablet and a laptop with the sidebar open.',
  'On the phone the toolbar keeps its four controls, but the overflow menu hides the zoom setting, so a reader has to open it twice to reach the text size.',
  'On the tablet in portrait the comment list opens as a sheet over the lower half of the page, which hides the paragraph the thread is about.',
  'The laptop is fine once the sidebar is closed; with it open the comment column drops below the text and the reader loses the line each thread refers to.',
  'I suggest we list these three widths in the test plan, note which controls must stay visible at each, and add the tablet case to the release checklist so it is checked before every build we share.',
].join(' ');

export const scrollAnchor = '.cm-content';

export function seed(page: Page, request: APIRequestContext) {
  return seedDocument(page, request, true);
}

/** The canvas with the Workspace panel, and the sidebar too if asked, tucked away: the canvas widths a laptop gives. */
async function closePanes(page: Page, sidebar: boolean) {
  if (sidebar) await page.getByRole('button', { name: 'Toggle sidebar', exact: true }).click();
  if (!(await page.locator('.pane').evaluate((element) => element.classList.contains('collapsed')))) await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
  await page.evaluate(() => new Promise<void>((resolve) => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
}

/** The cards beside the text, opened from the comments button if the canvas was narrower when the document loaded. */
async function showMargin(page: Page) {
  if (!(await page.locator('[data-margin-card]').count())) await page.locator('.comment-status').click();
  await expect(page.locator('[data-margin-card]').first()).toBeVisible();
}

export const variants = {
  // Too narrow for the column: a highlight opens its thread as a popover at the click.
  'panel-closed': { widths: [1440], apply: async (page: Page) => {
    await closePanes(page, false);
    await page.locator('.cm-comment-anchor', { hasText: 'narrow widths' }).first().click();
    await expect(page.locator('[data-popover]')).toBeVisible();
  } },
  'sidebar-and-panel-closed': { widths: [1440], apply: async (page: Page) => { await closePanes(page, true); await showMargin(page); } },
  // A thread picked from its highlight: its card keeps its line and its neighbours step aside.
  picked: { widths: [1440, 1920], apply: async (page: Page) => {
    if ((page.viewportSize()?.width ?? 0) < 1920) await closePanes(page, true);
    await showMargin(page);
    await page.locator('.cm-comment-anchor', { hasText: 'narrow widths' }).first().click();
    await expect(page.locator('[data-margin-card] .ct.focused')).toHaveCount(1);
  } },
  'resolved-open': { widths: [1440, 1920], apply: async (page: Page) => {
    if ((page.viewportSize()?.width ?? 0) < 1920) await closePanes(page, true);
    await page.locator('[data-resolved-entry]').click();
    // A resolved thread rests collapsed to its header; its comment shows once the reader opens it.
    await expect(page.locator('[data-resolved-column], .resolved-pop').getByText('Resolved · 1 message', { exact: true })).toBeVisible();
  } },
};

/** The review document, with every kind of comment at its extremes, or with none (the empty state). With the sidebar
 * collapsed, desktop widths that otherwise show the document alone show it beside the chat. */
export async function seedDocument(page: Page, request: APIRequestContext, withComments: boolean, collapseSidebar = false) {
  // Each capture runs on a fresh database, so the title needs no id to be told apart.
  const title = 'Responsive Design Review and Accessibility Coordination Agent';
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
  let personComments = 0;
  async function post(data: Record<string, unknown>) {
    const response = await request.post(`/api/artifacts/${artifact}/comments`, { params, data: { ...data, delivery_policy: 'immediate' } });
    expect(response.ok(), await response.text()).toBeTruthy();
    personComments++;
    return (await response.json()).comment.id as string;
  }
  async function comment(anchor: string, body: string, extra: Record<string, unknown> = {}) {
    const from = doc.indexOf(anchor);
    return post({ body, anchor_text: anchor, from_off: from, to_off: from + anchor.length, ...extra });
  }
  async function seedComments() {
    const agentComment = await comment('outstanding decisions', 'Which decisions need a separate review?');
    personComments--;
    isolatedDb(['connection.execute("UPDATE doc_comments SET author=?, author_kind=?, author_thread_id=? WHERE id=?", (sys.argv[2], "agent", sys.argv[3], sys.argv[4]))'], title, thread, agentComment);
    await post({ body: 'A separate review would slow the release; cover them in this one.', parent_id: agentComment });
    await post({ body: longReply, parent_id: agentComment });
    await comment(image, 'The title should remain legible on small screens.', { region: { x: 0.15, y: 0.15, w: 0.4, h: 0.2 } });
    // Many threads on neighbouring lines.
    await comment('Review checklist', 'Should the checklist come before the image?');
    await comment('controls remain visible', 'Which controls count here?');
    await comment('narrow widths', 'Name the widths we test.');
    await comment('alongside the document', 'Side by side, or below at narrow widths?');
    await comment('remains legible', 'Legible against which contrast target?');
    // Two open suggestions from each of two people, so each has an Accept all.
    await comment('Read the comments', 'Suggested wording.', { replacement: 'Read every comment' });
    await comment('Confirm that the page', 'Shorter.', { replacement: 'Confirm the page' });
    const otherSuggestions = [
      await comment('responsive review', 'Name the review.', { replacement: 'responsive design review' }),
      await comment('what remains to be decided', 'Plainer.', { replacement: 'what is still open' }),
    ];
    await comment('Navigation', 'Is this the only area with a note?');
    // The text this thread was on has since been deleted.
    const lost = await comment('compare the layout at each width', 'This paragraph needs a screenshot.');
    isolatedDb(['connection.execute("UPDATE doc_comments SET anchor_text=?, anchor_prefix=?, anchor_suffix=?, to_off=from_off WHERE id=?", tuple(sys.argv[2:6]))'], 'The export formats are listed in the appendix.', 'Appendix overview. ', ' See the appendix.', lost);
    const resolved = await comment(tableCell, 'This explanation now includes the expected behavior.');
    const patch = await request.patch(`/api/comments/${resolved}`, { params, data: { resolved: true } });
    expect(patch.ok(), await patch.text()).toBeTruthy();
    isolatedDb(['cursor = connection.execute("UPDATE doc_comments SET author=? WHERE artifact_id=? AND author_kind=?", (sys.argv[2], sys.argv[3], "user"))', 'assert cursor.rowcount == int(sys.argv[4]), cursor.rowcount'], 'Jordan Lee', artifact, String(personComments));
    isolatedDb(['cursor = connection.execute("UPDATE doc_comments SET author=? WHERE id IN (?, ?)", tuple(sys.argv[2:5]))', 'assert cursor.rowcount == 2, cursor.rowcount'], 'Sam Rivera', ...otherSuggestions);
  }
  if (withComments) await seedComments();
  async function open(target: Page) {
    await target.addInitScript(({ thread, artifact, docPath }) => {
      localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [thread]: { tabs: [{ instanceId: 'contact-sheet', artifactId: artifact, path: docPath, title: 'review.md', loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'contact-sheet', visible: true, activeSurface: 'artifact' } }, splitWidth: null }));
    }, { thread, artifact, docPath });
    await loginByUrl(target, { waitForComposer: false });
    if ((target.viewportSize()?.width ?? 0) <= 768) await target.getByRole('button', { name: 'Toggle sidebar' }).click();
    await sidebarThread(target, title).click();
    if (collapseSidebar && (target.viewportSize()?.width ?? 0) > 768) await target.getByRole('button', { name: 'Toggle sidebar' }).click();
    await expect(target.locator('.cm-content')).toBeVisible();
    const images = target.locator('.cm-md-image img, .cm-md-table td img');
    await expect(images).toHaveCount(2);
    for (let i = 0; i < 2; i++) await expect.poll(() => images.nth(i).evaluate((element: HTMLImageElement) => element.naturalWidth)).toBeGreaterThan(0);
  }
  await open(page);
  return open;
}
