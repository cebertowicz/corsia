import { mkdirSync, appendFileSync, writeFileSync } from 'node:fs';
import { test, expect } from '../support/test';
import { seed, showPicker } from '../contact-sheet/document-owner-supervisor';
import { setDebugVersionOverlay } from '../support/heldComments';
import type { Page } from '@playwright/test';

// Every frame starts from a newly opened page on one seeded instance, so no failure, focus or
// popover state carries from one frame to the next. Nothing here changes the stored ownership.
const out = process.env.O1_FRAMES_OUT!;
if (!out) throw new Error('Set O1_FRAMES_OUT to a fresh evidence directory');
const widths = (process.env.O1_WIDTHS ?? '375,393,899,1025,1280,1440').split(',').map(Number);

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
for (const workspace of ['closed', 'open'] as const) for (const width of widths) {
  test(`frames Workspace ${workspace} ${width}`, async ({ page, request, context }) => {
    test.setTimeout(240_000); page.setDefaultTimeout(10_000);
    const dir = `${out}/${workspace}`; mkdirSync(dir, { recursive: true });
    await page.setViewportSize({ width, height: 900 });
    await setDebugVersionOverlay(request, false);
    const open = await seed(page, request);
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
      const reach = parseFloat(style.outlineWidth) + parseFloat(style.outlineOffset);
      const trail: string[] = []; let clip = { left: 0, right: innerWidth, top: 0, bottom: innerHeight };
      for (let node = button.parentElement; node; node = node.parentElement) {
        const s = getComputedStyle(node);
        if (/(hidden|clip|scroll|auto)/.test(s.overflowX + s.overflowY)) { const r = node.getBoundingClientRect(); trail.push(`${node.className}:${Math.round(r.left)}-${Math.round(r.right)}`); clip = { left: Math.max(clip.left, r.left), right: Math.min(clip.right, r.right), top: Math.max(clip.top, r.top), bottom: Math.min(clip.bottom, r.bottom) }; }
      }
      return { trail, button: [Math.round(box.left), Math.round(box.right)], focusVisible: button.matches(':focus-visible'), reach, room: Math.min(box.left - clip.left, clip.right - box.right, box.top - clip.top, clip.bottom - box.bottom) };
    });
    log('focus', ring);
    expect(ring.focusVisible).toBe(true);
    expect(ring.room, 'the whole ring is inside the clip').toBeGreaterThanOrEqual(ring.reach);
    await shot(focused, 'focus');
    await focused.close();

    // Picker: its own page.
    const picker = await fresh();
    await showPicker(picker);
    await expect(picker.getByRole('combobox', { name: 'Supervisor', exact: true })).toBeVisible();
    await expect(picker.locator('[role=dialog] p')).toHaveCount(0);
    await shot(picker, 'picker');
    await picker.close();

    // Save states at the two reference widths: pending then failure, then refusal, each on its own page.
    if (width !== 1440 && width !== 393) return;
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
  });
}
