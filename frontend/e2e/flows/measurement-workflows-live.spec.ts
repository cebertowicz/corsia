import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { loginByUrl, openSettings, TEST_TOKEN } from '../support/corsia';
import { createAgent, createProject, selectProjectScope } from '../support/measurement-fixture';

function seed(...threads: string[]) {
 const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
 const workspace = process.env.CORSIA_E2E_WORKSPACE;
 const state = process.env.CORSIA_E2E_STATE_DIR;
 if (!python || !workspace || !state || process.env.CORSIA_E2E_MOCK !== '1') throw new Error('Disposable e2e-local state is required');
 return JSON.parse(execFileSync(python, ['frontend/e2e/support/workflow-live-seed.py', ...threads], {
  cwd: path.resolve('..'), encoding: 'utf8',
  env: { ...process.env, PYTHONPATH: 'backend', CORSIA_WORKSPACE: workspace, CORSIA_STATE_DIR: state, CORSIA_TEST_MODE: '1', CORSIA_ANALYTICS_DISABLE: '1' },
 }).trim().split('\n').at(-1)!) as { runs: string[]; detail: { phase: string; ref: string }[] };
}

test('an empty project shows the empty workflow state', async ({ page, request }) => {
 const project = await createProject(request, `Empty workflow proof ${Date.now()}`);
 await page.setViewportSize({ width: 1440, height: 1000 });
 await loginByUrl(page, { waitForComposer: false });
 await openSettings(page);
 await page.getByRole('link', { name: 'Measurement', exact: true }).click();
 await selectProjectScope(page, project.name);
 await expect(page.getByTestId('measurement-workflows')).toContainText('No work has run under a workflow yet');
});

test('workflow records reach the Measurement section and its plan', async ({ page, request }) => {
 test.setTimeout(360000);
 const project = await createProject(request, `Workflow proof ${Date.now()}`);
 const thread = await createAgent(request, 'Workflow proof agent', project.id);
 const second = await createAgent(request, 'Workflow second agent', project.id);
 const created = seed(thread, second);
 const response = await request.get(`/api/measurement/workflows?project=${project.id}&agent=none&grain=days&timezone=UTC&token=${encodeURIComponent(TEST_TOKEN)}`);
 expect(response.ok()).toBeTruthy();
 const body = await response.json();
 const entry = body.workflows.find((item: { ref: string }) => item.ref === 'e2e-close');
 expect(entry).toBeTruthy();
 const closeRuns = created.detail.filter((run) => run.ref === 'e2e-close').map((run) => run.phase);
 expect(entry.history.map((run: { run_id: string }) => run.run_id).sort()).toEqual(closeRuns.sort());
 expect(new Set(entry.history.map((run: { version_id: string }) => run.version_id)).size).toBe(2);
 expect(entry.history.some((run: { caught: string }) => run.caught === 'agent_closed')).toBeTruthy();
 expect(entry.history.some((run: { caught: string }) => run.caught === 'sent_back')).toBeTruthy();
 expect(entry.weekly.length).toBeGreaterThan(0);
 await page.setViewportSize({ width: 1440, height: 1000 });
 await loginByUrl(page, { waitForComposer: false });
 await openSettings(page);
 await page.getByRole('link', { name: 'Measurement', exact: true }).click();
 await selectProjectScope(page, project.name);
 const section = page.getByTestId('measurement-workflows');
 await expect(section).toContainText('e2e-close');
 await section.getByRole('button', { name: 'e2e-close', exact: true }).click();
 const detail = page.getByTestId('workflow-detail');
 await expect(detail).toBeVisible();
 await expect(detail).toContainText('Which step fails');
 await expect(page.getByRole('img', { name: /First-try success by week/ }).first()).toBeVisible();
 for (const version of entry.versions.slice(-2)) {
  const share = version.measures.first_try.value;
  if (share !== null) await expect(detail).toContainText(`${Math.round(share * 100)}%`);
 }
 const failedCount = detail.locator('tr').filter({ hasText: 'Gather statements' }).getByRole('button').first();
 await failedCount.scrollIntoViewIfNeeded();
 await failedCount.click();
 await expect(page.getByTestId('workflow-runs')).toContainText('Gather statements failed');
 await page.getByTestId('workflow-runs').getByRole('button').first().click();
 await expect(page.locator('.thread-plan-pane')).toBeVisible();
 await expect(page.locator('.thread-plan-pane')).toContainText('Gather statements');
});
