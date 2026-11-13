import path from 'node:path';
import { expect, type Locator, type Page } from './test';

/** Where chat-comment frames are written, when a run captures them. */
export const framesDirectory = process.env.CHAT_COMMENT_DIRECT_FRAMES;

/** Screenshot `target` fully drawn, on screen, uncovered and inside the viewport, logging its geometry. */
export async function capture(page: Page, target: Locator, name: string) {
  await expect(target).toBeVisible();
  await expect.poll(() => target.evaluate(element => {
    let opacity = 1;
    for (let node: HTMLElement | null = element as HTMLElement; node; node = node.parentElement) opacity *= Number(getComputedStyle(node).opacity);
    return opacity;
  })).toBe(1);
  const geometry = await target.evaluate(element => {
    const rect = element.getBoundingClientRect();
    const center = document.elementFromPoint(rect.x + rect.width / 2, rect.y + rect.height / 2);
    return { x: rect.x, y: rect.y, width: rect.width, height: rect.height, viewportWidth: innerWidth, viewportHeight: innerHeight, documentWidth: document.documentElement.scrollWidth, centerUncovered: !!center && (element === center || element.contains(center)) };
  });
  expect(geometry.width).toBeGreaterThan(0);
  expect(geometry.height).toBeGreaterThan(0);
  expect(geometry.x).toBeGreaterThanOrEqual(0);
  expect(geometry.x + geometry.width).toBeLessThanOrEqual(geometry.viewportWidth + 1);
  expect(geometry.y).toBeGreaterThanOrEqual(0);
  expect(geometry.y + geometry.height).toBeLessThanOrEqual(geometry.viewportHeight + 1);
  expect(geometry.documentWidth).toBeLessThanOrEqual(geometry.viewportWidth + 1);
  expect(geometry.centerUncovered).toBe(true);
  const stamp = await page.locator('.version-overlay').innerText();
  console.log(`FRAME ${name} ${JSON.stringify({ stamp, geometry })}`);
  await page.screenshot({ path: path.join(framesDirectory!, `${name}.png`), animations: 'disabled' });
}

/** Select `quote` inside the message's rendered body, as a person drags across it, then open its comment box unless told not to. */
export async function selectPhrase(page: Page, messageId: string, quote: string, open = true) {
  await page.evaluate(([id, phrase]) => {
    const body = document.querySelector<HTMLElement>(`[data-message-id="${id}"] .long-message-body`)!;
    const start = body.textContent!.indexOf(phrase);
    const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT), range = document.createRange();
    let position = 0, found = false;
    for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
      if (!found && start < position + node.length) { range.setStart(node, start - position); found = true; }
      if (found && start + phrase.length <= position + node.length) { range.setEnd(node, start + phrase.length - position); break; }
      position += node.length;
    }
    const selection = getSelection()!; selection.removeAllRanges(); selection.addRange(range);
  }, [messageId, quote]);
  if (open) await page.locator('.sel-comment').click();
}
