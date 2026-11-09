import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { loginByUrl, openSettings, TEST_TOKEN } from '../support/corsia';
import { createAgent, createProject, selectProjectScope } from '../support/measurement-fixture';

function seed(thread: string) {
 const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
 const workspace = process.env.CORSIA_E2E_WORKSPACE;
 const state = process.env.CORSIA_E2E_STATE_DIR;
 if (!python || !workspace || !state || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Disposable e2e-local state is required');
 return JSON.parse(execFileSync(python, ['frontend/e2e/support/workflow-live-seed.py', thread], {
  cwd: path.resolve('..'), encoding: 'utf8',
  env: { ...process.env, PYTHONPATH: 'backend', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state, CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1' },
 }).trim().split('\n').at(-1)!) as { runs: string[]; thread: string };
}

test('an empty project shows the empty workflow state', async ({ page, request }) => {
 const project = await createProject(request, `Empty workflow proof ${Date.now()}`);
 await page.setViewportSize({ width: 1440, height: 1000 });
 await loginByUrl(page, { waitForComposer: false });
 await openSettings(page);
 await page.getByRole('link', { name: 'Measurement', exact: true }).click();
 await selectProjectScope(page, project.name);
 const section = page.getByTestId('measurement-workflows');
 await expect(section).toContainText('No work has run under a workflow yet');
 const screenshotDir = process.env.CORSIA_WORKFLOW_FRAMES;
 if (screenshotDir && process.env.CORSIA_E2E_WORKFLOW_ONE !== '1') {
  mkdirSync(screenshotDir, { recursive: true });
  await section.evaluate((element) => element.scrollIntoView({ block: 'start' }));
  await page.screenshot({ path: path.join(screenshotDir, 'empty.png') });
 }
});

test('workflow records reach the Measurement section and its plan', async ({ page, request }) => {
 const project = await createProject(request, `Workflow proof ${Date.now()}`);
 const thread = await createAgent(request, 'Workflow proof agent', project.id);
 const oneVersion = process.env.CORSIA_E2E_WORKFLOW_ONE === '1';
 const created = seed(thread);
 const response = await request.get(`/api/measurement/workflows?project=${project.id}&agent=none&grain=days&timezone=UTC&token=${encodeURIComponent(TEST_TOKEN)}`);
 expect(response.ok()).toBeTruthy();
 const body = await response.json();
 const entry = body.workflows.find((item: { ref: string }) => item.ref === 'e2e-close');
 expect(entry).toBeTruthy();
 expect(entry.runs.map((run: { run_id: string }) => run.run_id).sort()).toEqual(created.runs.sort());
 expect(new Set(entry.runs.map((run: { version_id: string }) => run.version_id)).size).toBe(oneVersion ? 1 : 2);
 if (!oneVersion) {
  expect(entry.runs.some((run: { caught: string }) => run.caught === 'agent_closed')).toBeTruthy();
  expect(entry.runs.some((run: { caught: string }) => run.caught === 'sent_back')).toBeTruthy();
 }
 expect(entry.weekly.length).toBeGreaterThan(0);
 await page.setViewportSize({ width: 1440, height: 1000 });
 await loginByUrl(page, { waitForComposer: false });
 await openSettings(page);
 await page.getByRole('link', { name: 'Measurement', exact: true }).click();
 await selectProjectScope(page, project.name);
 const section = page.getByTestId('measurement-workflows');
 await expect(section).toContainText('e2e-close');
 await expect(page.getByTestId('workflow-detail')).toBeVisible();
 await expect(page.getByTestId('workflow-detail')).toContainText('Which step fails');
 const firstTryChart = page.getByRole('img', { name: /First-try success by week/ }).first();
 await expect(firstTryChart.locator('text').filter({ hasText: '100%' })).toHaveCount(1);
 const screenshotDir = process.env.CORSIA_WORKFLOW_FRAMES;
 if (screenshotDir) {
  mkdirSync(screenshotDir, { recursive: true });
  for (const [width, height, theme] of [[1440, 1000, 'light'], [1440, 1000, 'dark'], [390, 936, 'light'], [390, 936, 'dark']] as const) {
   await page.setViewportSize({ width, height });
   await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
   await section.evaluate((element) => element.scrollIntoView({ block: 'start' }));
   await page.screenshot({ path: path.join(screenshotDir, `${oneVersion ? 'one-version' : 'typical'}-${width}-${theme}.png`) });
  }
 }
 if (oneVersion) {
  await expect(page.getByTestId('workflow-detail')).toContainText('Nothing to compare yet');
  return;
 }
 await page.setViewportSize({ width: 1440, height: 1000 });
 await page.evaluate(() => document.documentElement.setAttribute('data-theme', 'light'));
 const failedCount = page.getByTestId('workflow-detail').locator('tr').filter({ hasText: 'Gather statements' }).getByRole('button').first();
 await failedCount.scrollIntoViewIfNeeded();
 if (screenshotDir) await page.screenshot({ path: path.join(screenshotDir, 'steps-and-agents.png') });
 await failedCount.click();
 await expect(page.getByTestId('workflow-runs')).toContainText('Gather statements failed');
 if (screenshotDir) await page.screenshot({ path: path.join(screenshotDir, 'run-list.png'), animations: 'disabled' });
 await page.getByTestId('workflow-runs').getByRole('button').first().click();
 await expect(page.locator('.thread-plan-pane')).toBeVisible();
 await expect(page.locator('.thread-plan-pane')).toContainText('Gather statements');
 if (screenshotDir) await page.screenshot({ path: path.join(screenshotDir, 'run-plan.png') });
});
