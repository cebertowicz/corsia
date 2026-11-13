import { test } from '../support/test';
import { expect } from '@playwright/test';
import { agentComment, reviewerSession } from '../support/liveConcurrency';
import { openLiveDocument, seedLiveDocument } from '../support/liveDocument';
import { setDebugVersionOverlay } from '../support/heldComments';
import { expectDiscussionReachableByScroll, expectNoCardScroller } from '../support/discussionVisible';


const quote = 'target line for a reply';
const text = Array.from({ length: 45 }, (_, i) => i === 25 ? quote : `document line ${i}`).join('\n\n');
for (const [width, height, project] of [[900, 900, 'chromium-desktop'], [900, 420, 'chromium-desktop'], [393, 852, 'chromium-mobile'], [393, 420, 'chromium-mobile']] as const) for (const theme of ['light', 'dark'] as const) {
 test(`long thread reply stays usable at ${width}x${height} ${theme}`, async ({ page, request }, info) => {
  test.skip(info.project.name !== project);
  await page.setViewportSize({ width, height });
  await setDebugVersionOverlay(request, false);
  const doc = await seedLiveDocument(request, `reply-visible-${width}-${height}-${theme}`, text);
  const reviewer = await reviewerSession(request, `${width}-${height}-${theme}`);
  await agentComment(request, doc, reviewer.id, quote, 'Existing thread. The narrow layout keeps the toolbar, but the comment column moves below the text and the reader loses the line each thread refers to. The narrow layout keeps the toolbar, but the comment column moves below the text.');
  await openLiveDocument(page, doc, 'document line 0');
  await page.evaluate(value => { document.documentElement.dataset.theme = value; }, theme);
  const scroller = page.locator('.cm-scroller');
  await scroller.evaluate(e => { e.scrollTop = e.scrollHeight * 0.4; });
  await expect(page.locator('.cm-line').filter({ hasText: quote })).toBeVisible();
  await scroller.evaluate(e => {
   const target = [...document.querySelectorAll<HTMLElement>('.cm-line')].find(n => n.textContent?.includes('target line for a reply'))!;
   e.scrollTop += target.getBoundingClientRect().top - (e.getBoundingClientRect().bottom - 120);
  });
  await page.locator('.cm-comment-anchor').filter({ hasText: quote }).click();
  const input = page.getByPlaceholder('Reply…');
  const send = page.getByRole('button', { name: 'Send reply' });
  const card = input.locator('xpath=ancestor::*[contains(concat(" ", normalize-space(@class), " "), " ct ")][1]');
  // The field grows with its text and never scrolls itself; the card never scrolls inside itself; the caret line (the end of the text) and Send stay on screen.
  /** Types at the end of the reply, as a person does after scrolling up to read: the product, not the test, scrolls the field's end back. */
  const typeAtEnd = async () => {
   await input.evaluate((field: HTMLTextAreaElement) => { field.focus({ preventScroll: true }); field.setSelectionRange(field.value.length, field.value.length); });
   await page.keyboard.insertText('.');
  };
  const checkOutcome = async () => {
   await expectNoCardScroller(card);
   await expect(send).toBeVisible();
   await expect.poll(async () => {
    const box = (await send.boundingBox())!;
    return box.y >= 0 && box.y + box.height <= height && box.x >= 0 && box.x + box.width <= width;
   }).toBe(true);
   await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => {
    const box = field.getBoundingClientRect(), line = Number.parseFloat(getComputedStyle(field).lineHeight) || 20;
    return box.bottom <= innerHeight + 1 && box.bottom - line >= -1;
   })).toBe(true);
   await send.click({ trial: true });
   if (width === 900 && height === 420) {
    const anchor = page.locator('.cm-comment-anchor').filter({ hasText: quote });
    const popover = input.locator('xpath=ancestor::*[@data-popover][1]');
    await expect.poll(async () => {
     const a = (await anchor.boundingBox())!, p = (await popover.boundingBox())!;
     return p.x + p.width <= a.x || a.x + a.width <= p.x || p.y + p.height <= a.y || a.y + a.height <= p.y;
    }).toBe(true);
   }
  };
  for (let i = 0; i < 40; i++) {
   await input.pressSequentially(`typed reply ${i}`);
   if (i < 39) await input.press('Shift+Enter');
  }
  await checkOutcome();
  await page.screenshot({ path: info.outputPath('typed-reply.png'), animations: 'disabled' });

  await input.fill(Array.from({ length: 40 }, (_, i) => `long reply line ${i}`).join('\n'));
  await input.focus();
  await page.keyboard.insertText('\nPasted ending');
  await checkOutcome();
  // Every line of the field is laid out: the field is as tall as its text.
  await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => field.scrollHeight <= field.clientHeight + 1)).toBe(true);
  await page.screenshot({ path: info.outputPath('long-reply.png'), animations: 'disabled' });

  await send.click();
  await expect(page.getByText('long reply line 0')).toBeVisible();
  await input.fill(Array.from({ length: 40 }, (_, i) => `next reply line ${i}`).join('\n'));
  // The card never scrolls inside itself; the surface holding it scrolls to every line of the discussion.
  await expectNoCardScroller(card);
  await expectDiscussionReachableByScroll(card, 'Existing thread');
  await expectDiscussionReachableByScroll(card, 'long reply line 0');
  await page.screenshot({ path: info.outputPath('second-filled.png'), animations: 'disabled' });
  if (width === 900 && height === 420) {
   // A long discussion must not consume the next reply's room or hide its Send button.
   await expect(input).toBeVisible();
   await input.fill(Array.from({ length: 40 }, (_, i) => `next reply line ${i}`).join('\n'));
   // The first line of both the comment and latest reply can be scrolled to in the popover; nothing inside the card scrolls.
   await expectNoCardScroller(card);
   for (const text of ['Existing thread', 'long reply line 0']) await expectDiscussionReachableByScroll(card, text);
   await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => field.clientHeight / parseFloat(getComputedStyle(field).lineHeight))).toBeGreaterThan(1.5);
   // Reading the discussion scrolled the field away; typing at its end brings the field's end and Send back by itself.
   await typeAtEnd();
   await expect.poll(async () => {
    const popover = (await input.locator('xpath=ancestor::*[@data-popover][1]').boundingBox())!;
    const button = (await send.boundingBox())!;
    return button.y >= popover.y && button.y + button.height <= popover.y + popover.height;
   }).toBe(true);
   await page.screenshot({ path: info.outputPath('second-filled.png'), animations: 'disabled' });
   await send.click();
   await expect(page.getByText('next reply line 0')).toBeVisible();
   await input.fill(Array.from({ length: 40 }, (_, i) => `third reply line ${i}`).join('\n'));
   // Each reply is reached by scrolling the popover; nothing inside the card scrolls.
   await expectNoCardScroller(card);
   for (const text of ['next reply line 0', 'long reply line 0']) await expectDiscussionReachableByScroll(card, text);
   await typeAtEnd();
   await expect(send).toBeInViewport({ ratio: 1 });
   await send.click({ trial: true });
  } else {
   await send.click();
   await expect(card).toContainText('next reply line 0');
  }

 });
}
