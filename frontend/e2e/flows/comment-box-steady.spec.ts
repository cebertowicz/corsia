import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { agentComment, reviewerSession } from '../support/liveConcurrency';
import { openLiveDocument, seedLiveDocument, serverText } from '../support/liveDocument';
import { beginComment, insertPrefix } from '../support/liveCommentProof';

test.use({ video: 'on' });
const quote = 'target line for comment';
const text = Array.from({ length: 60 }, (_, i) => i === 25 ? quote : `line ${i} with ordinary text`).join('\n\n');

async function place(page: Page, fraction: number) {
 const scroller = page.locator('.cm-scroller');
 await scroller.evaluate(e => { e.scrollTop = e.scrollHeight * 0.4; });
 const line = page.locator('.cm-line').filter({ hasText: quote });
 await expect(line).toBeVisible();
 await scroller.evaluate((e, fraction) => {
  const target = [...document.querySelectorAll<HTMLElement>('.cm-line')].find(n => n.textContent?.includes('target line for comment'))!;
  e.scrollTop += target.getBoundingClientRect().top - (e.getBoundingClientRect().top + e.getBoundingClientRect().height * fraction);
 }, fraction);
 return line;
}

for (const kind of ['draft', 'reply'] as const) {
 test(`@comment-box-steady ${kind} keeps its side through typing and a real remote edit`, async ({ page, browser, request }, info) => {
  test.skip(info.project.name !== 'chromium-desktop');
  test.setTimeout(120_000);
  await page.setViewportSize({ width: 1440, height: 900 });
  const doc = await seedLiveDocument(request, `steady-${kind}`, text);
  if (kind === 'reply') {
   const agent = await reviewerSession(request, `steady-${Date.now()}`);
   await agentComment(request, doc, agent.id, quote, 'Existing thread');
  }
  const writerContext = await browser.newContext({ viewport: { width: 1440, height: 900 } });
  try {
   const writer = await writerContext.newPage();
   await openLiveDocument(writer, doc, 'line 0 with ordinary text');
   await openLiveDocument(page, doc, 'line 0 with ordinary text');
   const line = await place(page, kind === 'draft' ? 0.68 : 0.65);
   if (kind === 'draft') await beginComment(page, quote);
   else {
    await page.locator('.cm-comment-anchor').filter({ hasText: quote }).click();
    await page.waitForTimeout(700);
    const before = (await line.boundingBox())!.y;
    await page.locator('.cm-scroller').evaluate(e => { e.scrollTop -= 100; });
    await expect.poll(async () => (await line.boundingBox())!.y).toBeGreaterThan(before + 50);
   }
   const input = page.getByPlaceholder(kind === 'draft' ? 'Comment…' : 'Reply…');
   await expect(input).toBeVisible(); await input.focus();
   const sample = async () => {
    const card = (await page.locator('[data-popover]').boundingBox())!;
    const anchor = (await line.boundingBox())!;
    return { card, anchor, side: card.y + card.height / 2 < anchor.y + anchor.height / 2 ? 'above' : 'below' };
   };
   const initial = await sample();
   console.log(`initial ${kind}: ${JSON.stringify(initial)}`);
   expect(initial.side).toBe('below');
   for (let i = 0; i < 9; i++) {
    for (const key of `line ${i}\n`) {
     if (key === '\n') {
      await input.press('Enter');
      await page.waitForTimeout(200);
      await page.evaluate(() => window.dispatchEvent(new Event('popover-anchor-moved')));
     } else await input.pressSequentially(key);
     const current = await sample();
     expect(current.side, `after ${i}: ${JSON.stringify(current)}`).toBe(initial.side);
     await expect(input).toBeFocused();
    }
    if (i === 1) {
     const before = (await line.boundingBox())!.y;
     await insertPrefix(writer, 'remote prefix\n');
     await expect.poll(() => serverText(request, doc)).toContain('remote prefix');
     await expect.poll(async () => (await line.boundingBox())!.y).not.toBe(before);
    }
   }
  } finally { await writerContext.close(); }
 });
}

test('@comment-box-steady phone uses a sheet while the new comment grows', async ({ page, request }, info) => {
 test.skip(info.project.name !== 'chromium-mobile');
 await page.setViewportSize({ width: 393, height: 852 });
 const doc = await seedLiveDocument(request, 'steady-phone', `lead text\n${quote}\ntail text\n`);
 await openLiveDocument(page, doc);
 await beginComment(page, quote);
 const input = page.getByPlaceholder('Comment…');
 await expect(page.locator('[data-popover] .composer')).toHaveCount(0);
 await expect(page.getByRole('dialog', { name: 'Document comments' })).toBeVisible();
 for (let i = 0; i < 9; i++) { await input.pressSequentially(`line ${i}`); await input.press('Enter'); await expect(input).toBeFocused(); }
});
