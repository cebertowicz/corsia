import { existsSync } from 'node:fs';
import { test, expect } from '../support/test';
import { seed } from '../contact-sheet/document-owner-supervisor';

test('seed and hold reviewer surface', async ({ page, request, browser }) => {
  test.setTimeout(0);
  const stopFile = process.env.O1_STOP_FILE;
  if (!stopFile || existsSync(stopFile)) throw new Error('Set a fresh stop-file path');
  await page.setViewportSize({ width: 1440, height: 1000 });
  await seed(page, request);
  const owner = await page.evaluate(() => Object.keys(JSON.parse(localStorage.getItem('corsia:artifactWorkspace:v1')!).threads)[0]);
  const url = new URL(`/thread/${owner}`, page.url()).toString();
  console.log(`O1_REVIEW_URL=${url}`);
  console.log('In Workspace Files, click Project planning.md.');
  const fresh = await browser.newContext();
  try {
    const reviewer = await fresh.newPage();
    await reviewer.goto(url);
    const document = reviewer.getByText('Project planning.md', { exact: true });
    await expect(document).toBeVisible();
    console.log('O1_FILES_SEED=visible in fresh browser context');
    await document.click();
    await expect(reviewer.locator('.cm-content')).toBeVisible();
    console.log('O1_FILES_OPEN=passed');
  } catch (error) {
    console.error(`O1_FILES_CHECK=failed: ${String(error)}`);
  } finally { await fresh.close(); }
  while (!existsSync(stopFile)) await new Promise(resolve => setTimeout(resolve, 1000));
});
