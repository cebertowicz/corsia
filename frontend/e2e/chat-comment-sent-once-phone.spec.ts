import { expect, test } from './support/test';
import { seedMockResponses, setMockDeliveryDelay } from './support/mock';
import { commentedMessage, QUOTE } from './support/chatCommentedMessage';
import { openCommentThread } from './support/chatComments';
import { framesWithBoth } from './support/sentOnceFrames';

/**
 * On a phone a reply is typed in the thread's sheet and shows in the same sheet once it is stored. The server
 * answers the post once the input is stored, so a slow provider start after that must not hold the reply in its
 * field while the sheet's thread already shows it. (A phone's root comment cannot do this: its box and the thread
 * sheet are never up together, `ChatCommentSelection.svelte` draws the sheet only while no box is open.)
 */
const DELAY_SECONDS = 2;

test('a phone reply is never in the sheet field and the sheet thread together', async ({ page, request }, info) => {
  test.skip(process.env.CORSIA_E2E_MOCK !== '1' || info.project.name !== 'chromium-mobile', 'Requires the disposable mock instance on the phone project');
  test.setTimeout(120_000);
  const { row, narrow } = await commentedMessage(page, request, 393);
  expect(narrow).toBe(true);
  const sheet = await openCommentThread(row, page, QUOTE, { tap: true });
  await expect(sheet.getByText('A note on the phrase')).toBeVisible();
  const panel = page.locator('[data-adaptive-drawer] .drawer-panel');
  await expect(panel.locator('.drawer-done')).toHaveText('Close');
  await expect(panel).toHaveCSS('border-top-left-radius', '28px');
  await sheet.getByPlaceholder('Reply…').fill('Draft reply');

  // The provider start after the input is stored takes seconds, as on a loaded machine.
  await seedMockResponses(request, [{ content: 'Received reply' }]);
  await setMockDeliveryDelay(request, DELAY_SECONDS);
  try {
    const reply = await framesWithBoth(page, 'Draft reply', async () => {
      await sheet.getByRole('button', { name: 'Send reply' }).tap();
      await expect(sheet.getByText('Draft reply')).toBeVisible();
      await expect(sheet.getByPlaceholder('Reply…')).toHaveValue('');
    });
    expect(reply.inThread, 'the reply reaches the sheet thread').toBeGreaterThan(0);
    expect(reply.both, 'frames that showed the reply in its field and in the sheet thread at once').toBe(0);
  } finally {
    await setMockDeliveryDelay(request, 0);
  }
});
