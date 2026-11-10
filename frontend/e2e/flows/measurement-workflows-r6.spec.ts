import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { loginByUrl, openSettings, TEST_TOKEN } from '../support/corsia';
import { createAgent, createProject, selectProjectScope } from '../support/measurement-fixture';

test('capture live workflow states', async ({ page, request }, testInfo) => {
 test.setTimeout(360000);
 const dir = process.env.CORSIA_WORKFLOW_FRAMES;
 const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
 const workspace = process.env.CORSIA_E2E_WORKSPACE;
 const state = process.env.CORSIA_E2E_STATE_DIR;
 if (!dir || !python || !workspace || !state || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Use isolated e2e-local provisioning and name the frame directory');
 mkdirSync(dir, { recursive: true });
 const project = await createProject(request, `Workflow capture ${Date.now()}`);
 const first = await createAgent(request, 'Billing agent', project.id);
 const second = await createAgent(request, 'Payroll agent', project.id);
 const spawned = await request.post('/api/delegation/threads', {
  params: { token: TEST_TOKEN },
  data: { title: 'Workflow worker', model: 'mock/test-model', thread_kind: 'subagent', spawned_by: first },
 });
 expect(spawned.ok()).toBeTruthy();
 const worker = (await spawned.json()).thread_id as string;
 execFileSync(python, ['frontend/e2e/support/workflow-live-seed.py', first, second, worker], {
  cwd: path.resolve('..'), encoding: 'utf8',
  env: { ...process.env, PYTHONPATH: 'backend', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state, CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1' },
 });
 const response = await request.get('/api/measurement/workflows', {
  params: { project: project.id, agent: 'none', grain: 'days', timezone: 'UTC', token: TEST_TOKEN },
 });
 expect(response.ok()).toBeTruthy();
 const body = await response.json();
 const entry = body.workflows.find((item: { ref: string }) => item.ref === 'e2e-close');
 expect(entry).toBeTruthy();
 if (process.env.CORSIA_E2E_WORKFLOW_ONE !== '1') {
  expect(body.workflows.length).toBeGreaterThanOrEqual(3);
  expect(body.workflows.find((item: { ref: string }) => item.ref === 'e2e-close').owner?.id).toBe(first);
  expect(body.workflows.find((item: { ref: string }) => item.ref === 'e2e-invoice').owner?.id).toBe(second);
  expect(body.workflows.find((item: { ref: string }) => item.ref === 'e2e-archive').runs).toHaveLength(0);
  const versions = [...new Set(entry.history.map((run: { version_id: string }) => run.version_id))];
  expect(versions).toHaveLength(2);
  expect(entry.history.filter((run: { version_id: string }) => run.version_id === versions[0]).length).toBeGreaterThanOrEqual(20);
  expect(entry.history.filter((run: { version_id: string }) => run.version_id === versions[1])).toHaveLength(10);
  expect(entry.verdict.word).toBe('collecting');
  expect(entry.verdict.inputs.baseline).toBe(versions[0]);
  expect(new Set(entry.history.map((run: { agent: { id: string } }) => run.agent?.id)).size).toBe(3);
  const workerRun = entry.runs.find((run: { agent: { id: string } }) => run.agent?.id === worker);
  expect(workerRun?.plan_target?.thread_id).toBe(first);
  expect(workerRun?.plan_target?.step_id).toBeTruthy();
  for (const caught of ['agent_closed', 'sent_back', 'corrected_later']) {
   expect(entry.history.some((run: { caught: string }) => run.caught === caught), caught).toBeTruthy();
  }
  expect(entry.history.some((run: { cost: number | null }) => run.cost !== null && run.cost > 0)).toBeTruthy();
  expect(entry.history.some((run: { minutes: number | null }) => run.minutes !== null && run.minutes > 0)).toBeTruthy();
 }
 const shot = (name: string) => page.screenshot({ path: path.join(dir, `${name}.png`), animations: 'disabled' });
 async function capture(name: string) {
  const scroller = page.locator('.measurement-scroller');
  await scroller.evaluate((el) => {
   const target = el.querySelector('[data-testid="measurement-workflows"]')!;
   el.scrollTop += target.getBoundingClientRect().top - el.getBoundingClientRect().top;
  });
  for (let i = 1; ; i++) {
   await shot(`${name}-${String(i).padStart(2, '0')}`);
   const moved = await scroller.evaluate((el) => { const before = el.scrollTop; el.scrollBy(0, 650); return el.scrollTop > before; });
   if (!moved) break;
  }
 }
 await page.setViewportSize({ width: 1440, height: 1000 });
 await loginByUrl(page, { waitForComposer: false });
 await openSettings(page);
 await page.getByRole('link', { name: 'Measurement', exact: true }).click();
 await selectProjectScope(page, project.name);
 const section = page.getByTestId('measurement-workflows');
 await expect(section).toContainText('e2e-close');
 const one = process.env.CORSIA_E2E_WORKFLOW_ONE === '1';
 if (!one) await section.getByRole('button', { name: 'e2e-close', exact: true }).click();
 else expect(body.workflows).toHaveLength(1);
 await expect(page.getByTestId('workflow-detail')).toBeVisible();
 if (one) {
  expect(new Set(entry.history.map((run: { version_id: string }) => run.version_id)).size).toBe(2);
  expect(entry.runs.length).toBeGreaterThan(0);
  await expect(page.getByTestId('workflow-detail')).toContainText('against');
  await capture('09-one-1440-light');
  return;
 }
 for (const [width, height, theme, name] of [
  [1440, 1000, 'light', '02-typical-1440-light'], [1440, 1000, 'dark', '03-typical-1440-dark'],
  [393, 936, 'light', '04-typical-393-light'], [393, 936, 'dark', '05-typical-393-dark'],
 ] as const) {
  await page.setViewportSize({ width, height });
  await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
  await capture(name);
 }
 await page.setViewportSize({ width: 1440, height: 1000 });
 await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
 const anchor = page.getByTestId('workflow-detail').locator('tr').filter({ hasText: 'Gather statements' }).getByRole('button').first();
 await anchor.scrollIntoViewIfNeeded();
 await anchor.click();
 await expect(page.getByTestId('workflow-runs')).toBeVisible();
 await shot('13-run-list-1440-light');
 await page.getByTestId('workflow-runs').getByRole('button').first().click();
 await expect(page.locator('.thread-plan-pane').getByRole('button', { name: /^Gather statements/ })).toBeFocused();
 await shot('14-plan-1440-light');
 await page.getByRole('link', { name: 'Measurement', exact: true }).click();
 await expect(page.getByTestId('workflow-detail')).toBeVisible();
 await expect(anchor).toBeFocused();
 await expect(anchor).toBeInViewport();
 await shot('15-back-1440-light');
 const workerAnchor = page.getByTestId('workflow-detail').locator('tr').filter({ hasText: 'Workflow worker' }).getByRole('button').first();
 await workerAnchor.scrollIntoViewIfNeeded();
 await workerAnchor.click();
 await page.getByTestId('workflow-runs').getByRole('button', { name: /Workflow worker/ }).click();
 await expect(page.locator('.thread-plan-pane').getByRole('button', { name: /^Delegated workflow/ })).toBeFocused();
 await page.getByRole('link', { name: 'Measurement', exact: true }).click();
 await expect(workerAnchor).toBeFocused();
 await page.setViewportSize({ width: 393, height: 936 });
 const mobileAnchor = page.getByTestId('workflow-detail').locator('tr').filter({ hasText: 'Gather statements' }).getByRole('button').first();
 await mobileAnchor.scrollIntoViewIfNeeded();
 if (testInfo.project.name === 'chromium-mobile') await mobileAnchor.tap();
 else await mobileAnchor.click();
 await expect(page.getByTestId('workflow-runs')).toBeVisible();
 await shot('16-run-list-393-touch');
 const mobileRun = page.getByTestId('workflow-runs').getByRole('button').first();
 if (testInfo.project.name === 'chromium-mobile') await mobileRun.tap();
 else await mobileRun.click();
 await expect(page.locator('.thread-plan-pane').getByRole('button', { name: /^Gather statements/ })).toBeFocused();
 await shot('17-plan-393-light');
 await page.getByRole('dialog', { name: /Plan/ }).getByRole('button', { name: 'Done', exact: true }).click();
 await page.getByRole('button', { name: 'Toggle sidebar', exact: true }).click();
 await expect(page.locator('aside.sidebar')).toHaveClass(/\bopen\b/);
 await page.getByRole('link', { name: 'Measurement', exact: true }).click();
 await expect(page.getByTestId('workflow-detail')).toBeVisible();
 await expect(mobileAnchor).toBeFocused();
 await expect(mobileAnchor).toBeInViewport();
 await shot('18-back-393-light');
 await workerAnchor.scrollIntoViewIfNeeded();
 if (testInfo.project.name === 'chromium-mobile') await workerAnchor.tap();
 else await workerAnchor.click();
 const workerRunButton = page.getByTestId('workflow-runs').getByRole('button', { name: /Workflow worker/ });
 if (testInfo.project.name === 'chromium-mobile') await workerRunButton.tap();
 else await workerRunButton.click();
 await expect(page.locator('.thread-plan-pane').getByRole('button', { name: /^Delegated workflow/ })).toBeFocused();
 await page.getByRole('dialog', { name: /Plan/ }).getByRole('button', { name: 'Done', exact: true }).click();
 await page.getByRole('button', { name: 'Toggle sidebar', exact: true }).click();
 await page.getByRole('link', { name: 'Measurement', exact: true }).click();
 await expect(workerAnchor).toBeFocused();
 await page.setViewportSize({ width: 1440, height: 1000 });
 await page.getByRole('button', { name: /Showing:/ }).click();
 await page.getByRole('option', { name: /Billing agent/ }).click();
 await expect(page.getByText('Agent: Billing agent')).toBeVisible();
 await section.scrollIntoViewIfNeeded();
 await shot('09-filtered-populated-1440-light-01');
 await page.getByRole('button', { name: 'Remove agent filter' }).click();
 const empty = await createProject(request, `Empty workflow capture ${Date.now()}`);
 await selectProjectScope(page, empty.name);
 await expect(section).toContainText('No work has run under a workflow yet');
 await section.scrollIntoViewIfNeeded();
 await shot('09-empty-1440-light-01');
 await page.route('**/api/measurement/workflows?**', (route) => route.fulfill({ status: 503, body: 'unavailable' }));
 await selectProjectScope(page, project.name);
 await expect(section).toContainText("Workflows couldn't load");
 await section.scrollIntoViewIfNeeded();
 await shot('09-error-1440-light-01');
});
