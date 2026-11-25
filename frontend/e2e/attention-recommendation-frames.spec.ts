import { test, expect } from './support/test';
import type { Page, TestInfo } from '@playwright/test';
import path from 'node:path';
import { openAppPastLanding } from './support/corsia';

const source = { thread_id: 'child', thread_label: 'Reviewer', project_id: null, project_label: null, parent_reviewed: true };
const cappedRationale = 'The recommended choice keeps the change reversible while making the next action obvious, so the person can decide now without creating another follow-up task or losing the context that explains why this option is a useful way forward today.';
function decision(over: Record<string, unknown> = {}) {
 return {
  id: 'decision-1', plan_id: 'plan-1', node_id: 'node-decision', source_node_id: 'node-source', thread_id: 'child',
  title: 'Where should decision state live?', context: 'Choose one canonical home.', tradeoff: 'A table adds another row.',
  pick: 'one', recommendation: 'B', rationale: 'Reuse the existing identity.',
  options: [{ id: 'A', label: 'Add a table', detail: 'Adds one table.' }, { id: 'B', label: 'Reuse the identity', detail: 'No schema change.' }],
  other_enabled: true, order_key: 0, revision: 3, raised_at: '2026-08-02T10:00:00Z', source, ...over,
 };
}
async function openDecision(page: Page, width: number, detail: ReturnType<typeof decision>) {
 const item = { key: 'decision:one', external_id: detail.id, renderer: 'decision', kind: 'decision', title: detail.title, raised_at: detail.raised_at, source, open: null, rank: null, detail };
 await page.route('**/api/attention/queue**', async route => {
  if (!route.request().url().includes('view=activity')) return route.fallback();
  await route.fulfill({ json: { count: 1, items: [item], ranker: null, notifications: { count: 0, items: [] } } });
 });
 await openAppPastLanding(page);
 if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
 await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
 if (width === 393) await page.locator('[data-activity-row]').filter({ hasText: detail.title }).click();
 await expect(page.getByRole('heading', { name: detail.title })).toBeVisible();
}
async function capture(page: Page, info: TestInfo, name: string) {
 await page.screenshot({ path: path.join(process.env.PE_FRAME_DIR ?? info.outputDir, `${name}.png`), fullPage: true, animations: 'disabled' });
}
for (const [label, width, height, colorScheme] of [
 ['393-light', 393, 852, 'light'], ['393-dark', 393, 852, 'dark'],
 ['1440-light', 1440, 900, 'light'], ['1440-dark', 1440, 900, 'dark'],
] as const) {
 for (const pick of ['one', 'any'] as const) {
  test(`@surface:attention ${pick} recommendation frame (${label})`, async ({ page }, info) => {
   test.skip(info.project.name !== 'chromium-desktop');
   await page.setViewportSize({ width, height });
   await page.emulateMedia({ colorScheme, reducedMotion: 'reduce' });
   const detail = pick === 'one' ? decision() : decision({ pick: 'any', recommendation: ['A', 'B'], rationale: 'Both are ready.' });
   await openDecision(page, width, detail);
   await expect(page.getByText('Why', { exact: true })).toBeVisible();
   await expect(page.locator('.option-rationale')).toContainText(detail.rationale);
   if (pick === 'any') {
    const boxes = page.getByRole('checkbox');
    await expect(boxes).toHaveCount(2);
    await expect(boxes.nth(0)).not.toBeChecked();
    await expect(boxes.nth(1)).not.toBeChecked();
    await expect(page.getByText('Recommended', { exact: true })).toHaveCount(2);
   }
   await capture(page, info, `pick-${pick}-recommended-${label}`);
  });
 }
}
test('@surface:attention recommendation fallback frame (393 light)', async ({ page }, info) => {
 test.skip(info.project.name !== 'chromium-desktop');
 await page.setViewportSize({ width: 393, height: 852 });
 await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
 await openDecision(page, 393, decision({ recommendation: null, rationale: 'No recommendation yet.' }));
 await expect(page.locator('dl')).toContainText('Why');
 await expect(page.locator('dl')).toContainText('No recommendation yet.');
 await capture(page, info, 'no-recommendation-fallback-393-light');
});
test('@surface:attention recommendation cap frame (393 light)', async ({ page }, info) => {
 test.skip(info.project.name !== 'chromium-desktop');
 expect(cappedRationale).toHaveLength(240);
 await page.setViewportSize({ width: 393, height: 852 });
 await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
 await openDecision(page, 393, decision({ rationale: cappedRationale }));
 await expect(page.locator('.option-rationale')).toContainText(cappedRationale);
 await capture(page, info, 'pick-one-rationale-cap-393-light');
});
