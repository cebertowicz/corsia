import { expect, test } from '@playwright/test';
import { createSession, openAppByUrl, chatComposer, sendButton, TEST_TOKEN, watchSessionSubscriptions } from './support/corsia';
import { seedMockResponses } from './support/mock';
import { focusSetting } from './support/focusOutcomeProof';

test.afterEach(async ({ page }) => {
  await focusSetting(page, true);
});

for (const width of [1280, 393]) {
  test(`live chip order matches reloaded history at ${width}px`, async ({ page, request }, testInfo) => {
    test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires disposable mock instance');
    await page.setViewportSize({ width, height: 900 });
    const title = `Ordering ${width} ${Date.now()}`;
    const threadId = await createSession(request, title);
    const subscriptions = watchSessionSubscriptions(page);
    await openAppByUrl(page);
    // Focus is on by default since #565 and hides finished work; this spec reads the raw transcript.
    await focusSetting(page, false);
    if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
    await page.getByText(title, { exact: true }).first().click();
    await subscriptions.waitForSubscribed(threadId);
    await seedMockResponses(request, [{ content: 'Before the chips' }]);
    await chatComposer(page).fill('First reply please');
    await sendButton(page).click();
    await expect(page.locator('.messages').getByText('Before the chips', { exact: true })).toBeVisible();
    const spawned = await request.post(`/api/delegation/threads?token=${TEST_TOKEN}`, {
      data: { title: 'Ordering worker', model: 'mock/test-model', thread_kind: 'subagent', spawned_by: threadId },
    });
    expect(spawned.ok()).toBeTruthy();
    await expect(page.locator('.messages').getByText('Ordering worker', { exact: true })).toBeVisible();
    await seedMockResponses(request, [{ content: 'After the chips' }]);
    await chatComposer(page).fill('Second reply please');
    await sendButton(page).click();
    const transcript = page.locator('.messages');
    await expect(transcript.getByText('After the chips', { exact: true })).toBeVisible();
    const assertOrder = async () => {
      const text = await transcript.innerText();
      const positions = ['Before the chips', 'Ordering worker', 'After the chips'].map(s => text.indexOf(s));
      // Name the transcript in the failure: a missing row and a misordered row need different fixes.
      const seen = JSON.stringify(text.slice(0, 600));
      expect(positions.every(p => p >= 0), `a row is missing from the transcript: ${seen}`).toBeTruthy();
      expect(positions, `rows are out of order in the transcript: ${seen}`).toEqual([...positions].sort((a, b) => a - b));
    };
    await assertOrder();
    await page.screenshot({ path: testInfo.outputPath(`live-${width}.png`), fullPage: true });
    await page.reload();
    await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
    if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
    await page.getByText(title, { exact: true }).first().click();
    await expect(transcript.getByText('After the chips', { exact: true })).toBeVisible();
    await assertOrder();
    await page.screenshot({ path: testInfo.outputPath(`reload-${width}.png`), fullPage: true });
  });
}
