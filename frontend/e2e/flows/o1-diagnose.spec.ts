import { mkdirSync } from 'node:fs';
import { test, expect } from '../support/test';
import { seed, showPicker } from '../contact-sheet/document-owner-supervisor';
import { setDebugVersionOverlay } from '../support/heldComments';

const out = '/Users/jarvis/Corsia/projects/corsia/.threads/a8f17a81-7687-4d22-8b07-819464800949/o1-pack/round2-frames';

test('ownership layout at constrained widths', async ({ page, request }) => {
  test.setTimeout(180_000);
  page.setDefaultTimeout(8_000);
  mkdirSync(out, { recursive: true });
  await page.setViewportSize({ width: 1440, height: 900 });
  await setDebugVersionOverlay(request, false);
  await seed(page, request);
  await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
  const widths = process.env.O1_WIDTHS?.split(',').map(Number) ?? [1024, 1100, 1280, 1440];
  for (const width of widths) {
    await page.setViewportSize({ width, height: 900 });
    await page.waitForTimeout(2000); // Capture the settled shell, not its resize transition.
    await expect(page.locator('.cm-content')).toBeVisible();
    await page.screenshot({ animations: 'disabled', path: `${out}/rest-${width}.png` });
    const details = page.getByRole('button', { name: 'Document details' });
    if (!(await page.locator('button.identity:visible').first().isVisible()) && await details.isVisible()) {
      await details.click();
      await expect(page.locator('button.identity:visible').first()).toBeVisible();
      await page.screenshot({ animations: 'disabled', path: `${out}/details-${width}.png` });
    }
    await showPicker(page);
    const control = page.locator('button.identity:visible').first();
    await expect(control).toBeVisible();
    const surface = page.getByRole('combobox', { name: 'Supervisor', exact: true });
    await expect(surface).toBeVisible();
    const rects = await page.evaluate(() => {
      const box = (el: Element | null) => el?.getBoundingClientRect().toJSON();
      const visibleControl = [...document.querySelectorAll('button.identity')].find(el => el.getBoundingClientRect().width > 0);
      return { pane: box(document.querySelector('.doc')), control: box(visibleControl ?? null), input: box(document.querySelector('#supervisor-choice')), dialog: box(document.querySelector('[role=dialog]')) };
    });
    console.log('WIDTH', width, JSON.stringify(rects));
    expect(rects.control!.width).toBeGreaterThan(30);
    if (width >= 769 && !(await control.evaluate(button => button.classList.contains('compact')))) {
      const labelsFit = await control.evaluate(button => {
        const parent = button.parentElement!.getBoundingClientRect();
        return [...button.querySelectorAll<HTMLElement>('.identity-part, .identity-name')].every(part => {
          const range = document.createRange();
          range.selectNodeContents(part);
          return [...range.getClientRects()].every(rect => rect.left >= parent.left - 1 && rect.right <= parent.right + 1)
            && (getComputedStyle(part).overflow === 'visible' || part.scrollWidth <= part.clientWidth + 1);
        });
      });
      expect(labelsFit).toBe(true);
    }
    if (await page.evaluate(() => matchMedia('(any-pointer: coarse)').matches)) expect(rects.control!.height).toBeGreaterThanOrEqual(44);
    expect(rects.input!.width).toBeGreaterThan(100);
    expect(rects.dialog!.left).toBeGreaterThanOrEqual(0);
    expect(rects.dialog!.right).toBeLessThanOrEqual(width);
    await page.screenshot({ animations: 'disabled', path: `${out}/picker-${width}.png` });
    await page.keyboard.press('Escape');
    if (await details.isVisible() && await details.getAttribute('aria-expanded') === 'true') await details.click();
  }
  // The base owner slot can be hidden by the shell at narrow pane widths.
  // Exercise save states at the last width where this run showed the slot.
  await showPicker(page);
  let release!: () => void;
  const delayed = new Promise<void>(resolve => release = resolve);
  await page.route('**/api/artifacts/*/ownership', async route => {
    await delayed;
    await route.fulfill({ status: 500, contentType: 'text/plain', body: 'Internal Server Error' });
  });
  await page.getByRole('option', { name: 'Community outreach', exact: true }).click();
  await expect(page.locator('button.identity:visible').first()).toContainText('Saving');
  await expect(page.getByRole('combobox', { name: 'Supervisor', exact: true })).toHaveValue('Community outreach');
  await page.screenshot({ animations: 'disabled', path: `${out}/pending-open.png` });
  await page.keyboard.press('Escape');
  await expect(page.locator('button.identity:visible').first()).toContainText('Saving');
  await page.screenshot({ animations: 'disabled', path: `${out}/pending-closed.png` });
  release();
  await expect(page.locator('button.identity:visible').first()).toContainText('Change failed');
  await expect(page.locator('button.identity:visible').first()).toContainText('Research coordinator');
  await page.screenshot({ animations: 'disabled', path: `${out}/failure-closed.png` });
  await page.locator('button.identity:visible').first().click();
  await expect(page.getByText('Couldn’t change the supervisor. Try again.')).toBeVisible();
  await expect(page.locator('body')).not.toContainText('Internal Server Error');
  await page.screenshot({ animations: 'disabled', path: `${out}/failure-open.png` });
  await page.unroute('**/api/artifacts/*/ownership');
  await page.route('**/api/artifacts/*/ownership', route => route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'legacy raw refusal' }) }));
  await page.getByRole('combobox', { name: 'Owner', exact: true }).click();
  await page.getByRole('option', { name: 'You', exact: true }).click();
  await expect(page.getByText('Taking ownership isn’t available when more than one person uses this CORSIA. Choose an agent instead.')).toBeVisible();
  await page.screenshot({ animations: 'disabled', path: `${out}/refusal-fallback.png` });
});

test('ownership control at phone width', async ({ page, request }) => {
  await page.setViewportSize({ width: 1440, height: 900 });
  await setDebugVersionOverlay(request, false);
  await seed(page, request);
  await page.getByRole('button', { name: 'Toggle panel', exact: true }).click();
  await page.setViewportSize({ width: 393, height: 900 });
  await expect(page.locator('.cm-content')).toBeVisible();
  await page.screenshot({ animations: 'disabled', path: `${out}/rest-393.png` });
  await showPicker(page);
  await page.screenshot({ animations: 'disabled', path: `${out}/picker-393.png` });
});
