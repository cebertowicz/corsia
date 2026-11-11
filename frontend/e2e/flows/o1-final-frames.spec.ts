import { mkdirSync, appendFileSync, writeFileSync } from 'node:fs';
import { test, expect } from '../support/test';
import { seed, showPicker } from '../contact-sheet/document-owner-supervisor';
import { setDebugVersionOverlay } from '../support/heldComments';
import type { Page } from '@playwright/test';

// Every frame starts from a newly opened page on one seeded instance, so no failure, focus or
// popover state carries from one frame to the next. Nothing here changes the stored ownership.
const out = process.env.O1_FRAMES_OUT!;
if (!out) throw new Error('Set O1_FRAMES_OUT to a fresh evidence directory');
const widths = (process.env.O1_WIDTHS ?? '375,393,899,1025,1280,1372,1440').split(',').map(Number);

async function measure(page: Page) {
  return page.evaluate(() => {
    const button = [...document.querySelectorAll<HTMLElement>('button.identity')].find(el => el.getBoundingClientRect().width > 0)!;
    const box = button.getBoundingClientRect();
    const parts = [...button.querySelectorAll<HTMLElement>('.identity-part')].map(part => {
      const range = document.createRange(); range.selectNodeContents(part);
      const right = Math.max(...[...range.getClientRects()].map(rect => rect.right));
      const name = part.querySelector<HTMLElement>('.identity-name')!;
      return { text: part.textContent, right, truncated: name.scrollWidth > name.clientWidth + 1, ellipsis: getComputedStyle(name).textOverflow === 'ellipsis' };
    });
    return {
      width: box.width, right: box.right, parts,
      clearance: Math.min(...parts.map(part => box.right - part.right)),
      focusVisible: button.matches(':focus-visible'),
      text: button.textContent,
    };
  });
}

test.use({ seedPerTest: true });
// One seed for the whole set, so an agent keeps its id (and its avatar colour, derived from the id) in every frame.
test('frames', async ({ page, request, context }) => {
  test.setTimeout(3_000_000); page.setDefaultTimeout(10_000);
  await page.setViewportSize({ width: 1440, height: 900 });
  await setDebugVersionOverlay(request, false);
  const open = await seed(page, request);
  let pickerMinimum = 0;
  for (const workspace of ['closed', 'open'] as const) for (const width of widths) {
    const dir = `${out}/${workspace}`; mkdirSync(dir, { recursive: true });
    async function fresh() {
      const target = await context.newPage(); target.setDefaultTimeout(10_000);
      // Closing the Workspace panel is a desktop control, so a closed frame loads at 1440 and closes it, then
      // takes the frame's width, as the width sweep does. An open frame loads at its own width.
      await target.setViewportSize({ width: workspace === 'closed' ? 1440 : width, height: 900 });
      await open(target);
      if (workspace === 'closed') {
        await target.getByRole('button', { name: 'Toggle panel', exact: true }).click();
        await target.setViewportSize({ width, height: 900 });
      }
      await target.waitForTimeout(2000);
      await expect(target.locator('.cm-content')).toBeVisible();
      return target;
    }
    async function shot(target: Page, name: string) {
      await expect(target.locator('body')).not.toContainText('Internal Server Error');
      await target.screenshot({ animations: 'disabled', path: `${dir}/${name}-${width}.png` });
    }
    const log = (name: string, value: unknown) => appendFileSync(`${out}/measure.jsonl`, `${JSON.stringify({ workspace, width, frame: name, ...(value as object) })}\n`);

    // Rest: a newly loaded page, nothing focused, no failure text.
    const rest = await fresh();
    const details = rest.getByRole('button', { name: 'Document details' });
    if (!(await rest.locator('button.identity:visible').first().isVisible()) && await details.isVisible()) await details.click();
    const restMeasure = await measure(rest);
    log('rest', restMeasure);
    expect(restMeasure.focusVisible, 'no focus ring at rest').toBe(false);
    expect(restMeasure.text).not.toMatch(/failed|Saving/);
    await shot(rest, 'rest');
    await rest.close();

    // Keyboard focus: the ring sits outside the button, inside the slot's clip, so it never covers the text.
    const focused = await fresh();
    if (!(await focused.locator('button.identity:visible').first().isVisible()) && await focused.getByRole('button', { name: 'Document details' }).isVisible()) await focused.getByRole('button', { name: 'Document details' }).click();
    await focused.keyboard.press('Shift');
    await focused.locator('button.identity:visible').first().focus();
    const ring = await focused.evaluate(() => {
      const button = [...document.querySelectorAll<HTMLElement>('button.identity')].find(el => el.getBoundingClientRect().width > 0)!;
      const box = button.getBoundingClientRect(); const style = getComputedStyle(button);
      const offset = parseFloat(style.outlineOffset), thickness = parseFloat(style.outlineWidth);
      // The ring's inner edge is the button box grown by its offset; the glyphs are measured against it.
      const inner = { left: box.left - offset, right: box.right + offset, top: box.top - offset, bottom: box.bottom + offset };
      const range = document.createRange(); range.selectNodeContents(button);
      // Text cut by the button's own clip (an ellipsized name) is not drawn past the button, so the visible text ends there.
      const rects = [...range.getClientRects()].filter(rect => rect.width > 0).map(rect => ({ left: Math.max(rect.left, box.left), right: Math.min(rect.right, box.right), top: rect.top, bottom: rect.bottom }));
      const gap = { left: Math.min(...rects.map(r => r.left)) - inner.left, right: inner.right - Math.max(...rects.map(r => r.right)), top: Math.min(...rects.map(r => r.top)) - inner.top, bottom: inner.bottom - Math.max(...rects.map(r => r.bottom)) };
      let clip = { left: 0, right: innerWidth, top: 0, bottom: innerHeight };
      for (let node = button.parentElement; node; node = node.parentElement) {
        const s = getComputedStyle(node);
        if (/(hidden|clip|scroll|auto)/.test(s.overflowX + s.overflowY)) { const r = node.getBoundingClientRect(); clip = { left: Math.max(clip.left, r.left), right: Math.min(clip.right, r.right), top: Math.max(clip.top, r.top), bottom: Math.min(clip.bottom, r.bottom) }; }
      }
      const reach = offset + thickness;
      return { focusVisible: button.matches(':focus-visible'), offset, thickness, gap, room: Math.min(box.left - clip.left, clip.right - box.right, box.top - clip.top, clip.bottom - box.bottom) - reach };
    });
    log('focus', ring);
    expect(ring.focusVisible).toBe(true);
    expect(ring.room, 'the whole ring is inside the clip').toBeGreaterThanOrEqual(0);
    for (const side of ['left', 'right', 'top', 'bottom'] as const) expect(ring.gap[side], `at least 2px between the ring and the text, ${side}`).toBeGreaterThanOrEqual(2);
    await shot(focused, 'focus');
    await focused.close();

    // Picker: its own page.
    const picker = await fresh();
    await showPicker(picker);
    await expect(picker.getByRole('combobox', { name: 'Supervisor', exact: true })).toBeVisible();
    await expect(picker.locator('[role=dialog] p')).toHaveCount(0);
    const card = await picker.locator('[role=dialog]').evaluate(dialog => {
      const box = dialog.getBoundingClientRect();
      const fields = ['owner-choice', 'supervisor-choice'].map(id => { const input = document.getElementById(id) as HTMLInputElement; return { id, value: input.value, clipped: input.scrollWidth > input.clientWidth }; });
      return { width: box.width, left: box.left, right: box.right, viewport: innerWidth, fields };
    });
    log('picker', card);
    if (workspace === 'closed' && width === 1025) pickerMinimum = card.width;
    if (workspace === 'open' && [1025, 1280, 1372].includes(width)) {
      expect(card.width, 'popover is at least its width at 1025').toBeGreaterThanOrEqual(Math.min(360, width - 16) - 1);
      expect(card.left, 'inside the viewport, 8px margin').toBeGreaterThanOrEqual(8 - 0.5);
      expect(card.right, 'inside the viewport, 8px margin').toBeLessThanOrEqual(card.viewport - 8 + 0.5);
      for (const field of card.fields) expect(field.clipped, `${field.value} is not clipped`).toBe(false);
    }
    await shot(picker, 'picker');
    await picker.close();

    // Save states at the two reference widths: pending then failure, then refusal, each on its own page.
    if (width !== 1440 && width !== 393) continue;
    const saving = await fresh();
    await showPicker(saving);
    let release!: () => void;
    const delayed = new Promise<void>(resolve => release = resolve);
    await saving.route('**/api/artifacts/*/ownership', async route => { await delayed; await route.fulfill({ status: 500, contentType: 'text/plain', body: 'Internal Server Error' }); });
    await saving.locator('#supervisor-options').getByRole('option', { name: 'Community outreach', exact: true }).click();
    await expect(saving.locator('button.identity:visible').first()).toContainText('Saving');
    await shot(saving, 'pending-open');
    // Close with the mouse, as a person who opened it with the mouse would.
    await saving.locator('.document-header').click({ position: { x: 4, y: 4 } });
    await expect(saving.getByRole('combobox', { name: 'Supervisor', exact: true })).toBeHidden();
    await expect(saving.locator('button.identity:visible').first()).toContainText('Saving');
    log('pending-closed', await measure(saving));
    await shot(saving, 'pending-closed');
    release();
    await expect(saving.locator('button.identity:visible').first()).toContainText('Change failed');
    await expect(saving.locator('button.identity:visible').first()).toContainText('Research coordinator');
    log('failure-closed', await measure(saving));
    await shot(saving, 'failure-closed');
    await saving.locator('button.identity:visible').first().click();
    await expect(saving.getByText('Couldn’t change the supervisor. Try again.')).toBeVisible();
    await expect(saving.getByRole('combobox', { name: 'Supervisor', exact: true })).toBeFocused();
    await shot(saving, 'failure-open');
    await saving.close();

    const refused = await fresh();
    await showPicker(refused);
    await refused.route('**/api/artifacts/*/ownership', route => route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'legacy raw refusal' }) }));
    await refused.getByRole('combobox', { name: 'Owner', exact: true }).click();
    await refused.locator('#owner-options').getByRole('option', { name: 'You', exact: true }).click();
    await expect(refused.getByText('Taking ownership isn’t available when more than one person uses this CORSIA. Choose an agent instead.')).toBeVisible();
    await shot(refused, 'refusal-fallback');
    await refused.close();
  }
});
