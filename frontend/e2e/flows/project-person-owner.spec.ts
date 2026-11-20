import path from 'node:path';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { e2eModel, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { isolatedDb } from '../support/heldComments';
import { ownerItem, rolePicker } from '../support/documentOwnerSupervisor';

for (const [label, width, height] of [['wide', 1440, 900], ['phone', 393, 852]] as const) {
 test(`project instruction ownership persists at ${label}`, async ({ page, request }, info) => {
  test.skip(info.project.name !== (label === 'wide' ? 'chromium-desktop' : 'chromium-mobile'));
  test.setTimeout(120_000);
  await page.setViewportSize({ width, height });
  const params = { token: TEST_TOKEN };
  const name = `Garden project ${Date.now()}`;
  const created = await request.post('/api/projects', { params, data: { name, description: 'Garden', status: 'active' } });
  expect(created.ok(), await created.text()).toBeTruthy();
  const list = await (await request.get('/api/projects', { params: { ...params, status: 'active' } })).json();
  const project = list.projects.find((item: { name: string }) => item.name === name);
  expect(project).toBeTruthy();
  const thread = await request.post('/api/threads', { params, data: { title: 'Garden planner', project_id: project.id, model: e2eModel() } });
  expect(thread.ok(), await thread.text()).toBeTruthy();
  const threadId: string = (await thread.json()).thread_id;
  const docPath = path.join(project.path, 'docs', 'garden.md');
  const doc = await request.put('/api/files/content', { params, data: { path: docPath, text: '# Garden plan\n\nPrepare the garden.\n', thread_id: threadId, create: true } });
  expect(doc.ok(), await doc.text()).toBeTruthy();
  const artifact: string = (await doc.json()).artifact.id;
  isolatedDb([
   "connection.execute(\"INSERT OR IGNORE INTO users (id,password_hash,created_at,password_changed_at) VALUES ('second-person','x','2026-01-01','2026-01-01')\")",
   "connection.execute(\"UPDATE artifact_registrations SET parent_thread_id=? WHERE artifact_id=?\", ('project:' + sys.argv[3], sys.argv[2]))",
   "connection.execute(\"UPDATE threads SET agent_enabled=1,agent_status='active' WHERE id=?\", (sys.argv[4],))",
   "connection.execute(\"UPDATE projects SET conductor_thread_id=? WHERE id=?\", (sys.argv[4],sys.argv[3]))",
  ], artifact, project.id, threadId);
  await page.addInitScript(({ threadId, artifact, docPath }) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({ threads: { [threadId]: { tabs: [{ instanceId: 'garden', artifactId: artifact, path: docPath, title: 'garden.md', loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'garden', visible: true, activeSurface: 'artifact' } } })), { threadId, artifact, docPath });
  await loginByUrl(page, { waitForComposer: false });
  if (width < 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await page.getByRole('button', { name: /Jump to/ }).click();
  await page.locator('.palette').getByRole('button', { name, exact: true }).click();
  if (width < 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await page.getByRole('navigation', { name: 'Agents' }).getByRole('button', { name: 'Garden planner' }).click();
  await expect(ownerItem(page)).toHaveAccessibleName('Owner and supervisor: Garden planner');
  await ownerItem(page).click();
  await rolePicker(page).getByRole('option', { name: /^You/ }).click();
  await expect(ownerItem(page)).toHaveAccessibleName('Owner: You');
  await page.reload();
  if (width < 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await page.getByRole('button', { name: /Jump to/ }).click();
  await page.locator('.palette').getByRole('button', { name, exact: true }).click();
  if (width < 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
  await page.getByRole('navigation', { name: 'Agents' }).getByRole('button', { name: 'Garden planner' }).click();
  await expect(ownerItem(page)).toHaveAccessibleName('Owner: You');
  expect(isolatedDb(["print(connection.execute(\"SELECT parent_thread_id FROM artifact_registrations WHERE artifact_id=? AND relationship_state IN ('owner-active','owner-pinned')\", (sys.argv[2],)).fetchone()[0])"], artifact).trim()).toBe('ui');
 });
}
