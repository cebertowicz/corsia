import type { Page } from '@playwright/test';

/**
 * Sample every frame until `act` resolves; count the frames that showed `note` in a text field and in a comment
 * thread at once. A thread is a margin card, a popover or a sheet: a dialog named Comment.
 */
export async function framesWithBoth(page: Page, note: string, act: () => Promise<void>) {
  await page.evaluate((text) => {
    const state = { both: 0, inBox: 0, inThread: 0, running: true };
    (window as any).__sentOnce = state;
    const holds = (node: Element) => /^(TEXTAREA|INPUT)$/.test(node.tagName) && (node as HTMLTextAreaElement).value.includes(text);
    const sample = () => {
      const inBox = [...document.querySelectorAll('textarea, input')].some(holds);
      const inThread = [...document.querySelectorAll('.chat-margin-card, [role="dialog"][aria-label="Comment"]')]
        .some((card) => (card.textContent ?? '').includes(text));
      if (inBox) state.inBox++;
      if (inThread) state.inThread++;
      if (inBox && inThread) state.both++;
    };
    const tick = () => {
      sample();
      if (state.running) requestAnimationFrame(tick);
    };
    (state as any).sample = sample;
    requestAnimationFrame(tick);
  }, note);
  await act();
  return page.evaluate(() => {
    const state = (window as any).__sentOnce;
    // A slow runner can paint no frame between the move and the end of the act; the last sample reads the layout the act left.
    state.sample();
    state.running = false;
    const { both, inBox, inThread } = state;
    return { both, inBox, inThread } as { both: number; inBox: number; inThread: number };
  });
}
