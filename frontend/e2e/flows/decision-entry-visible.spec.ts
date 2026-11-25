import { test, expect } from '../support/test';
import { openAppPastLanding } from '../support/corsia';

const source = { thread_id: 'child', thread_label: 'Reviewer', project_id: null, project_label: null, parent_reviewed: true };
const detail = {
 id: 'decision-entry', plan_id: 'plan-1', node_id: 'node-decision', source_node_id: 'node-source',
 thread_id: 'child', title: 'Choose the next step', context: 'Choose what happens next.', tradeoff: '',
 pick: 'one', recommendation: 'A', rationale: 'Ready to proceed.', other_enabled: true,
 options: [{ id: 'A', label: 'Proceed', detail: '' }], order_key: 0, revision: 3,
 raised_at: '2026-11-24T10:00:00Z', source,
};
const item = { key: 'decision:entry', external_id: detail.id, renderer: 'decision', kind: 'decision', title: detail.title, raised_at: detail.raised_at, source, open: null, rank: null, detail };

for (const [width, height] of [[1440, 900], [900, 900], [900, 420], [393, 852], [393, 420], [393, 350]] as const) {
 for (const theme of ['light', 'dark'] as const) {
  test(`decision entry stays visible at ${width}x${height} ${theme}`, async ({ page }, info) => {
   test.skip(info.project.name !== 'chromium-desktop');
   await page.setViewportSize({ width, height: Math.max(height, 852) });
   await page.emulateMedia({ colorScheme: theme, reducedMotion: 'reduce' });
   await page.route('**/api/attention/queue**', async route => {
    if (!route.request().url().includes('view=activity')) return route.fallback();
    await route.fulfill({ json: { count: 1, items: [item], ranker: null, notifications: { count: 0, items: [] } } });
   });
   await openAppPastLanding(page);
   if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
   await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
   if (width === 393) await page.locator('[data-activity-row]').filter({ hasText: detail.title }).click();
   await page.getByRole('radio', { name: 'Something else' }).check();
   const input = page.getByRole('textbox', { name: 'Your answer' });
   await input.focus();
   await page.setViewportSize({ width, height });
   for (let i = 0; i < 8; i++) {
    await input.pressSequentially(`typed line ${i}`);
    if (i < 7) await input.press('Shift+Enter');
   }
   await input.press('Enter');
   await input.pressSequentially('another line');
   await expect(input).toHaveValue(`${Array.from({ length: 8 }, (_, i) => `typed line ${i}`).join('\n')}\nanother line`);
   await expect(page.getByRole('button', { name: 'Answer', exact: true })).toBeVisible();
   await page.screenshot({ path: info.outputPath('decision-typed.png'), animations: 'disabled' });
   await input.fill(Array.from({ length: 40 }, (_, i) => `decision line ${i}`).join('\n'));
   await expect.poll(async () => {
    const field = (await input.boundingBox())!;
    const actions = (await page.getByRole('button', { name: 'Answer', exact: true }).boundingBox())!;
    return field.y >= 0 && field.y + field.height <= actions.y && actions.y + actions.height <= height;
   }).toBe(true);
   await page.screenshot({ path: info.outputPath('decision-long.png'), animations: 'disabled' });
  });
 }
}
