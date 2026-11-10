import { expect, type Page, type APIRequestContext } from '@playwright/test';
import { chatComposer, createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from '../support/corsia';
import { injectMessage, seedMockResponses } from '../support/mock';
import { commentEditor, commentEditorClosed, commentOnMessage } from '../support/chatComments';

export const scrollAnchor = '.user-bubble';
// Each capture runs on a fresh database, so the title needs no id to be told apart.
export const title = 'Responsive review of the document view';

export const conversation: Array<[role: 'user' | 'assistant', content: string]> = [
  ['user', 'Please review the document view at different screen sizes. In particular, check that the table stays readable and that long comments open without covering the text around them.'],
  ['assistant', 'I opened the review document at phone, tablet and desktop widths. The table wraps its long cell and keeps the other columns identifiable. Long comments open beside the text on a wide screen and in a sheet on a phone.'],
  ['user', 'What happens to the navigation while the document scrolls?'],
  ['assistant', 'The navigation stays at the top of the canvas while the document scrolls under it. On a phone it tucks into the menu button, so the text gets the whole width.'],
  ['user', 'And in the dark theme?'],
  ['assistant', 'Labels and controls keep their contrast against the dark background. The one weak spot is the table border, which fades at the lowest brightness; I suggest a stronger border colour there.'],
  ['user', 'Can a reader get back to the latest message after reading older context?'],
  ['assistant', 'Yes. Scrolling up shows a control that returns to the latest message, and the composer stays in reach the whole time.'],
];

export const summary = [
  'The review covers the document, the comment list, the navigation and the message composer across the available screen sizes.',
  'At a narrow width the document stays readable without a sideways scroll. A long table cell wraps while the columns next to it stay identifiable, and an image keeps its proportions.',
  'In the dark theme, labels and controls stay distinct from their backgrounds. Comments carry enough context for a reader to understand the question without reopening the whole discussion.',
  'Compare the first and last parts of the conversation as well. The composer should stay within reach, and the reader should be able to return to the latest message after reviewing earlier context.',
].join('\n\n');

/** Comments on a phrase of a message the way a person does: selects its text, then writes on the selection menu. */
async function commentOn(page: Page, messageId: string, phrase: string, note: string) {
  const row = page.locator(`[data-message-id="${messageId}"]`);
  await row.scrollIntoViewIfNeeded();
  await row.evaluate((element, phrase) => {
    const body = element.querySelector('.long-message-body') ?? element;
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
    let node = walker.nextNode() as Text | null;
    while (node && !node.data.includes(phrase)) node = walker.nextNode() as Text | null;
    if (!node) throw new Error(`"${phrase}" was not rendered`);
    const start = node.data.indexOf(phrase);
    const range = document.createRange(); range.setStart(node, start); range.setEnd(node, start + phrase.length);
    const selection = getSelection()!; selection.removeAllRanges(); selection.addRange(range);
  }, phrase);
  await page.locator('.sel-comment').click();
  await commentEditor(page).fill(note);
  await page.getByRole('button', { name: 'Comment', exact: true }).click();
  await commentEditorClosed(page);
  return row;
}

export async function seed(page: Page, request: APIRequestContext) {
  const thread = await createSession(request, title);
  const ids: string[] = [];
  for (const [role, content] of conversation) ids.push(await injectMessage(request, thread, { role, content, metadata: role === 'assistant' ? { for_user: true } : {} }, TEST_TOKEN));
  const last = await injectMessage(request, thread, { role: 'assistant', content: summary, metadata: { for_user: true } }, TEST_TOKEN);
  async function open(target: Page) {
    await openAppPastLanding(target);
    if ((target.viewportSize()?.width ?? 0) <= 768) await target.getByRole('button', { name: 'Toggle sidebar' }).click();
    await sidebarThread(target, title).click();
    await expect(target.locator('.cm-comment-anchor').first()).toBeVisible();
    await expect(target.locator('button.model-chip .model-chip-name')).toHaveText('Claude Sonnet 5.5');
  }
  // The person's comments are written on a desktop window, then delivered with a message, as they are in use.
  const captureSize = page.viewportSize();
  await page.setViewportSize({ width: 1440, height: 900 });
  await openAppPastLanding(page);
  await sidebarThread(page, title).click();
  await waitForWsConnected(page);
  const table = await commentOn(page, ids[5], 'a stronger border colour', 'Which colour, and does it pass contrast in both themes?');
  const closing = await commentOn(page, last, 'return to the latest message', 'Is that control reachable by keyboard?');
  await seedMockResponses(request, [{ content: 'A darker grey border (the divider colour) passes in both themes; I will apply it. The control is a button in the tab order, so Tab reaches it.' }]);
  await chatComposer(page).fill('Two notes on the review.');
  await page.getByRole('button', { name: 'Send message' }).click();
  await commentOnMessage(table, 'a stronger border colour');
  await commentOnMessage(closing, 'return to the latest message');
  if (captureSize) await page.setViewportSize(captureSize);
  await open(page);
  return open;
}
