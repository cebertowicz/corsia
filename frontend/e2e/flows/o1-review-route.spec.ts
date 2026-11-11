import { existsSync } from 'node:fs';
import { test, expect } from '../support/test';
import { seed } from '../contact-sheet/document-owner-supervisor';
import { TEST_TOKEN } from '../support/corsia';

test('seed and hold reviewer surface', async ({ page, request, browser }) => {
  test.setTimeout(0);
  const stopFile = process.env.O1_STOP_FILE;
  if (!stopFile || existsSync(stopFile)) throw new Error('Set a fresh stop-file path');
  await page.setViewportSize({ width: 1440, height: 1000 });
  await seed(page, request);
  const url = new URL('/', page.url()).toString();
  console.log(`O1_REVIEW_URL=${url}`);
  console.log('Select Release planning in the sidebar, open Workspace Files, and click Project planning.md.');
  const fresh = await browser.newContext({ viewport: { width: 1440, height: 1000 } });
  try {
    const reviewer = await fresh.newPage();
    await reviewer.goto(`${url}?token=${TEST_TOKEN}`);
    await expect(reviewer.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
    await reviewer.locator('aside.sidebar').getByText('Release planning', { exact: true }).click();
    const document = reviewer.getByText('Project planning.md', { exact: true });
    await expect(document).toBeVisible();
    console.log('O1_FILES_SEED=visible in fresh browser context');
    await document.click();
    await expect(reviewer.locator('.cm-content')).toBeVisible();
    await expect(reviewer.locator('button.identity:visible').first()).toBeVisible();
    console.log('O1_FILES_OPEN=passed; ownership row visible');
  } catch {
    console.error('O1_FILES_CHECK=failed; fresh browser did not reach the seeded document');
  } finally { await fresh.close(); }
  while (!existsSync(stopFile)) await new Promise(resolve => setTimeout(resolve, 1000));
});
