import { test, expect } from './support/test';
import type { Page } from '@playwright/test';
import { openAppPastLanding } from './support/corsia';

const source = { thread_id: 'child', thread_label: 'Reviewer subagent', project_id: null, project_label: null, parent_reviewed: false };
function decision(id: string, title: string) {
 const detail = {
  id, plan_id: 'plan-1', node_id: `node-${id}`, source_node_id: 'node-source', thread_id: 'child', title,
  context: 'A decision needs your answer.', tradeoff: 'Both options have a cost.', pick: 'one', recommendation: 'A',
  rationale: 'The first option is ready.', options: [{ id: 'A', label: 'Proceed', detail: '' }, { id: 'B', label: 'Wait', detail: '' }],
  other_enabled: true, order_key: 0, revision: 3, raised_at: '2026-11-24T10:00:00Z', source,
 };
 return { key: `decision:${id}`, external_id: id, renderer: 'decision', kind: 'decision', title, raised_at: detail.raised_at, source, open: null, rank: null, detail };
}
const first = decision('decision-1', 'Where should decision state live?');
const second = decision('decision-2', 'Choose another step');
async function openActivity(page: Page, width: number, readFailed: () => boolean = () => false) {
 await page.route('**/api/attention/queue**', async route => {
  if (!route.request().url().includes('view=activity')) return route.fallback();
  if (readFailed()) return route.fulfill({ status: 500, json: {} });
  await route.fulfill({ json: { count: 2, items: [first, second], ranker: null, notifications: { count: 0, items: [] } } });
 });
 await openAppPastLanding(page);
 if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
 await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
 if (width === 393) await page.locator('[data-activity-row]').filter({ hasText: first.title }).click();
 await expect(page.getByRole('heading', { name: first.title })).toBeVisible();
}
for (const width of [1440, 393]) {
 test(`@surface:attention a subagent decision is answerable in Activity at ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
  let sent: Record<string, unknown> | null = null;
  await page.route('**/api/plans/decisions/decision-1/answer', async route => {
   sent = route.request().postDataJSON();
   await route.fulfill({ status: 200, json: {} });
  });
  await openActivity(page, width);
  await expect(page.getByRole('region', { name: 'Decision' }).getByRole('button', { name: /Reviewer subagent/ })).toBeVisible();
  await expect(page.getByText('Raised directly — no parent review.')).toBeVisible();
  await expect(page.getByText('A decision needs your answer.')).toBeVisible();
  await page.getByRole('radio', { name: 'Something else' }).check();
  await page.getByRole('textbox', { name: 'Your answer' }).fill('Actually, wait.');
  await page.getByRole('radio', { name: /Proceed/ }).check();
  const answer = page.getByRole('button', { name: 'Answer', exact: true });
  await expect(answer).toBeEnabled();
  await answer.click();
  await expect.poll(() => sent).not.toBeNull();
  expect(sent).toMatchObject({ option_id: 'A', other_text: null, expected_revision: 3 });
 });
 test(`@surface:attention a failed refresh identifies last known items at ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
  let failRead = false;
  await page.route('**/api/plans/decisions/decision-1/answer', async route => {
   failRead = !failRead;
   await route.fulfill({ status: 422, json: { detail: 'Please review your answer.' } });
  });
  await openActivity(page, width, () => failRead);
  await page.getByRole('radio', { name: /Proceed/ }).check();
  await page.getByRole('button', { name: 'Answer', exact: true }).click();
  await expect(page.getByText('Showing last known items.').filter({ visible: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: first.title })).toBeVisible();
  await page.getByRole('button', { name: 'Answer', exact: true }).click();
  await expect(page.getByText('Showing last known items.')).toHaveCount(0);
 });
 test(`@surface:attention the shared queue failure and recovery show in Activity at ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
  await openActivity(page, width);
  let fail = true;
  await page.route('**/api/attention/queue', route => fail
   ? route.fulfill({ status: 503, json: {} })
   : route.fulfill({ json: { current: null, count: 0, items: [] } }));
  const reload = () => page.evaluate(async () => {
   const state = await import('/src/lib/stores/attentionQueueState.svelte.ts');
   await state.reload();
  });
  await reload();
  await expect(page.getByText('Showing last known items.').filter({ visible: true })).toBeVisible();
  await expect(page.getByRole('heading', { name: first.title })).toBeVisible();
  fail = false;
  await reload();
  await expect(page.getByText('Showing last known items.')).toHaveCount(0);
 });
 test(`@surface:attention a decision draft survives changing items at ${width}`, async ({ page }) => {
  await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
  await openActivity(page, width);
  await page.getByRole('radio', { name: 'Something else' }).check();
  await page.getByRole('textbox', { name: 'Your answer' }).fill('Half-typed answer');
  if (width === 393) await page.locator('main').getByRole('button', { name: 'Activity', exact: true }).click();
  await page.locator('[data-activity-row]').filter({ hasText: second.title }).click();
  await expect(page.getByRole('heading', { name: second.title })).toBeVisible();
  if (width === 393) await page.locator('main').getByRole('button', { name: 'Activity', exact: true }).click();
  await page.locator('[data-activity-row]').filter({ hasText: first.title }).click();
  await expect(page.getByRole('textbox', { name: 'Your answer' })).toHaveValue('Half-typed answer');
 });
}
