import { test, expect } from './support/test';
import type { Page } from '@playwright/test';
import { openAppPastLanding } from './support/corsia';

const source = { thread_id: 'child', thread_label: 'Reviewer', project_id: null, project_label: null, parent_reviewed: true };
const detail = {
 id: 'footer-decision', plan_id: 'plan-1', node_id: 'node-decision', source_node_id: 'node-source', thread_id: 'child',
 title: 'Choose the next step', context: 'Choose what happens next.', tradeoff: '', pick: 'one', recommendation: 'A',
 rationale: 'Ready to proceed.', other_enabled: true,
 options: [{ id: 'A', label: 'Proceed', detail: '' }, { id: 'B', label: 'Wait', detail: '' }],
 order_key: 0, revision: 3, raised_at: '2026-11-24T10:00:00Z', source,
};
const item = { key: 'decision:footer', external_id: detail.id, renderer: 'decision', kind: 'decision', title: detail.title, raised_at: detail.raised_at, source, open: null, rank: null, detail };
async function openDecision(page: Page, width: number) {
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
for (const [name, width, height, colorScheme] of [
 ['393-light', 393, 852, 'light'], ['393-dark', 393, 852, 'dark'], ['1440-light', 1440, 900, 'light'],
] as const) {
 test(`@surface:attention decision options stay reachable above actions (${name})`, async ({ page }, info) => {
  test.skip(info.project.name !== 'chromium-desktop');
  await page.setViewportSize({ width, height });
  await page.emulateMedia({ colorScheme, reducedMotion: 'reduce' });
  await openDecision(page, width);
  const options = page.getByRole('radio');
  for (let i = 0; i < await options.count(); i++) {
   await options.nth(i).scrollIntoViewIfNeeded();
   const option = (await options.nth(i).boundingBox())!;
   const action = (await page.getByRole('button', { name: 'Answer', exact: true }).boundingBox())!;
   expect(option.y + option.height).toBeLessThanOrEqual(action.y);
   await expect(options.nth(i)).toBeVisible();
  }
  await page.screenshot({ path: info.outputPath(`decision-footer-${name}.png`), animations: 'disabled' });
 });
}
for (const [name, keyboard] of [['keyboard-closed', 0], ['keyboard-open', 336]] as const) {
 test(`@surface:attention typing in Other keeps the whole box above the actions row (${name})`, async ({ page }, info) => {
  test.skip(info.project.name !== 'chromium-desktop');
  await page.setViewportSize({ width: 393, height: 852 });
  await page.emulateMedia({ reducedMotion: 'reduce' });
  await openDecision(page, 393);
  await page.getByRole('radio', { name: 'Something else' }).check();
  const other = page.getByRole('textbox', { name: 'Your answer' });
  await other.focus();
  if (keyboard) {
   await page.setViewportSize({ width: 393, height: 852 - keyboard });
   await page.evaluate(() => document.documentElement.style.setProperty('--kb-toolbar-inset', '60px'));
  }
  await page.keyboard.type('Move it to the bottom, and make sure the owner can read this answer.');
  await expect.poll(async () => {
   const box = (await other.boundingBox())!;
   const footer = (await page.getByRole('button', { name: 'Answer', exact: true }).boundingBox())!;
   return box.y >= 0 && box.y + box.height <= footer.y;
  }).toBe(true);
  await page.screenshot({ path: info.outputPath(`decision-other-${name}.png`), animations: 'disabled' });
 });
}
