import { test } from '../support/test';
import { expect } from '@playwright/test';
import { agentComment, reviewerSession } from '../support/liveConcurrency';
import { openLiveDocument, seedLiveDocument } from '../support/liveDocument';
import { setDebugVersionOverlay } from '../support/heldComments';
import { expectDiscussionVisible } from '../support/discussionVisible';


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
  // Test the outcome after filling the available room; an eight-line cap was only the old mechanism.
  const checkOutcome = async () => {
   await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => field.scrollHeight > field.clientHeight)).toBe(true);
   await expect(send).toBeVisible();
   for (const element of [send, card]) await expect.poll(async () => {
    const box = (await element.boundingBox())!;
    return box.y >= 0 && box.y + box.height <= height && box.x >= 0 && box.x + box.width <= width;
   }).toBe(true);
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
  if (width === 393 && height === 420) {
   await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => field.clientHeight)).toBeGreaterThan(45);
   await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => field.scrollTop)).toBeGreaterThan(0);
  }
  await page.screenshot({ path: info.outputPath('long-reply.png'), animations: 'disabled' });

  await send.click();
  await expect(page.getByText('long reply line 0')).toBeVisible();
  await input.fill(Array.from({ length: 40 }, (_, i) => `next reply line ${i}`).join('\n'));
  await expectDiscussionVisible(card, 'Existing thread');
  await expectDiscussionVisible(card, 'long reply line 0');
  await page.screenshot({ path: info.outputPath('second-filled.png'), animations: 'disabled' });
  if (width === 900 && height === 420) {
   // A long discussion must not consume the next reply's room or hide its Send button.
   await expect(input).toBeVisible();
   await input.fill(Array.from({ length: 40 }, (_, i) => `next reply line ${i}`).join('\n'));
   // The first line of both the comment and latest reply must be in the visible discussion above the composer.
   for (const text of ['Existing thread', 'long reply line 0']) {
    await expect.poll(() => card.evaluate((thread, sought) => {
     const walker = document.createTreeWalker(thread, NodeFilter.SHOW_TEXT);
     let node: Text | null;
     while ((node = walker.nextNode() as Text | null) && !node.data.includes(sought)) {}
     if (!node) return false;
     const start = node.data.indexOf(sought), range = document.createRange();
     range.setStart(node, start); range.setEnd(node, start + sought.length);
     const line = range.getBoundingClientRect(), popover = thread.closest('[data-popover]')!.getBoundingClientRect();
     const composer = thread.querySelector('.ct-compose')!.getBoundingClientRect();
     for (let parent = node.parentElement; parent && parent !== thread; parent = parent.parentElement) {
      if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(parent).overflowY)) continue;
      const box = parent.getBoundingClientRect();
      if (line.top < box.top || line.bottom > box.bottom) return false;
     }
     return line.height > 0 && line.top >= popover.top && line.bottom <= Math.min(popover.bottom, composer.top);
    }, text)).toBe(true);
   }
   await expect.poll(() => input.evaluate((field: HTMLTextAreaElement) => field.clientHeight / parseFloat(getComputedStyle(field).lineHeight))).toBeGreaterThan(1.5);
   await expect.poll(async () => {
    const popover = (await input.locator('xpath=ancestor::*[@data-popover][1]').boundingBox())!;
    const button = (await send.boundingBox())!;
    return button.y >= popover.y && button.y + button.height <= popover.y + popover.height;
   }).toBe(true);
   await page.screenshot({ path: info.outputPath('second-filled.png'), animations: 'disabled' });
   await send.click();
   await expect(page.getByText('next reply line 0')).toBeVisible();
   await input.fill(Array.from({ length: 40 }, (_, i) => `third reply line ${i}`).join('\n'));
   const replies = card.locator('[data-discussion-replies]');
   const lineIsInside = (sought: string) => replies.evaluate((region, text) => {
    const walker = document.createTreeWalker(region, NodeFilter.SHOW_TEXT);
    let node: Text | null;
    while ((node = walker.nextNode() as Text | null) && !node.data.includes(text)) {}
    if (!node) return false;
    const range = document.createRange(), start = node.data.indexOf(text);
    range.setStart(node, start); range.setEnd(node, start + text.length);
    const line = range.getBoundingClientRect(), box = region.getBoundingClientRect();
    return line.top >= box.top && line.bottom <= box.bottom;
   }, sought);
   await expect.poll(() => lineIsInside('next reply line 0')).toBe(true);
   // Scroll slightly past the first reply's metadata so its first text line is fully in the small region.
   await replies.evaluate(region => { region.scrollTop = 16; });
   await expect.poll(() => lineIsInside('long reply line 0')).toBe(true);
   await send.click({ trial: true });
   const original = card.locator('[data-discussion-original]');
   await original.evaluate(region => { region.scrollTop = region.scrollHeight; });
   await original.getByRole('button', { name: 'Show more' }).click();
   await expect(original.getByRole('button', { name: 'Show less' })).toBeVisible();
   await send.click({ trial: true });
  } else {
   await send.click();
   await expect(card).toContainText('next reply line 0');
  }

 });
}
