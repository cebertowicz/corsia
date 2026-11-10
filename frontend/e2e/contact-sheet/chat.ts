import { expect, type Page, type APIRequestContext } from '@playwright/test';
import { createSession, openAppPastLanding, sidebarThread, TEST_TOKEN } from '../support/corsia';
import { injectMessage } from '../support/mock';

export const scrollAnchor = '.user-bubble';

export async function seed(page: Page, request: APIRequestContext) {
  const title = `Review conversation ${Date.now()}`;
  const thread = await createSession(request, title);
  for (let index = 0; index < 12; index++) {
    await injectMessage(request, thread, {
      role: index % 2 ? 'assistant' : 'user',
      content: index % 2
        ? 'The review is complete. The navigation remains visible while the document scrolls, and each comment retains its original context. Please check the narrow layout and the dark theme before sharing the design.'
        : 'Please review the document view at different screen sizes. In particular, check that the table remains readable and that long comments can be opened without covering the surrounding content.',
      metadata: index % 2 ? { for_user: true } : {},
    }, TEST_TOKEN);
  }
  await injectMessage(request, thread, {
    role: 'assistant',
    content: [
      'The review covers the document, the comment drawer, the navigation rail, and the message composer across the available screen sizes.',
      'At a narrow width the document should remain readable without requiring a horizontal scroll. A long table cell should wrap while the adjacent columns remain identifiable, and an image should keep its proportions.',
      'In the dark theme, labels and controls should remain distinguishable from their backgrounds. Comments need enough context for a reader to understand the question without reopening the entire discussion.',
      'Please compare the first and last parts of the conversation as well. The composer should stay accessible, and the reader should be able to return to the latest message after reviewing earlier context.',
    ].join('\n\n'),
    metadata: { for_user: true },
  }, TEST_TOKEN);
  const quote = 'Please check the narrow layout and the dark theme before sharing the design.';
  await injectMessage(request, thread, {
    role: 'user',
    content: 'I added a note about the review.',
    metadata: { reply_context: { comments: [{ id: 'c1', seq: 1, from_off: 0, to_off: quote.length, quote, anchor_prefix: '', anchor_suffix: '', note: 'Can the table remain readable on a small phone?', cid: 'c1' }] } },
  }, TEST_TOKEN);
  async function open(target: Page) {
    await openAppPastLanding(target);
    if ((target.viewportSize()?.width ?? 0) <= 768) await target.getByRole('button', { name: 'Toggle sidebar' }).click();
    await sidebarThread(target, title).click();
    await expect(target.locator('.sent-comment')).toBeVisible();
    await expect(target.locator('button.model-chip .model-chip-name')).toHaveText('Claude Sonnet 5.5');
  }
  await open(page);
  return open;
}
