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
  const widths = process.env.O1_WIDTHS?.split(',').map(Number) ?? [1024, 1100, 1280, 1440, 393];
  for (const width of widths) {
    await page.setViewportSize({ width, height: 900 });
    await expect(page.locator('.cm-content')).toBeVisible();
    await page.screenshot({ path: `${out}/rest-${width}.png` });
    const details = page.getByRole('button', { name: 'Document details' });
    if (await details.isVisible()) {
      await details.click();
      await expect(page.locator('button.identity')).toBeVisible();
      await page.screenshot({ path: `${out}/details-${width}.png` });
    }
    await showPicker(page);
    const control = page.locator('button.identity');
    await expect(control).toBeVisible();
    const surface = page.getByRole('combobox', { name: 'Supervisor', exact: true });
    await expect(surface).toBeVisible();
    const rects = await page.evaluate(() => {
      const box = (el: Element | null) => el?.getBoundingClientRect().toJSON();
      return { pane: box(document.querySelector('.doc')), control: box(document.querySelector('button.identity')), input: box(document.querySelector('#supervisor-choice')), dialog: box(document.querySelector('[role=dialog]')) };
    });
    console.log('WIDTH', width, JSON.stringify(rects));
    expect(rects.control!.width).toBeGreaterThan(30);
    if (width >= 769) {
      const labelsFit = await control.evaluate(button => {
        const parent = button.closest('.document-metadata')!.getBoundingClientRect();
        return [...button.querySelectorAll('.identity-part')].every(part => {
          const range = document.createRange();
          range.selectNodeContents(part.firstChild!);
          const label = range.getBoundingClientRect();
          return label.left >= parent.left - 1 && label.right <= parent.right + 1;
        });
      });
      expect(labelsFit).toBe(true);
    }
    if (await page.evaluate(() => matchMedia('(any-pointer: coarse)').matches)) expect(rects.control!.height).toBeGreaterThanOrEqual(44);
    expect(rects.input!.width).toBeGreaterThan(100);
    expect(rects.dialog!.left).toBeGreaterThanOrEqual(0);
    expect(rects.dialog!.right).toBeLessThanOrEqual(width);
    await page.screenshot({ path: `${out}/picker-${width}.png` });
    await page.keyboard.press('Escape');
    if (await details.isVisible() && await details.getAttribute('aria-expanded') === 'true') await details.click();
  }
  await page.setViewportSize({ width: 900, height: 900 });
  await showPicker(page);
  let release!: () => void;
  const delayed = new Promise<void>(resolve => release = resolve);
  await page.route('**/api/artifacts/*/ownership', async route => {
    await delayed;
    await route.fulfill({ status: 500, contentType: 'text/plain', body: 'Internal Server Error' });
  });
  await page.getByRole('option', { name: 'Community outreach', exact: true }).click();
  await expect(page.locator('button.identity')).toContainText('Saving');
  await expect(page.getByRole('combobox', { name: 'Supervisor', exact: true })).toHaveValue('Community outreach');
  await page.screenshot({ path: `${out}/pending-open.png` });
  await page.keyboard.press('Escape');
  await expect(page.locator('button.identity')).toContainText('Saving');
  await page.screenshot({ path: `${out}/pending-closed.png` });
  release();
  await expect(page.locator('button.identity')).toContainText('Change failed');
  await expect(page.locator('button.identity')).toContainText('Research coordinator');
  await page.screenshot({ path: `${out}/failure-closed.png` });
  await page.locator('button.identity').click();
  await expect(page.getByText('Couldn’t change the supervisor. Try again.')).toBeVisible();
  await expect(page.locator('body')).not.toContainText('Internal Server Error');
  await page.screenshot({ path: `${out}/failure-open.png` });
  await page.unroute('**/api/artifacts/*/ownership');
  await page.route('**/api/artifacts/*/ownership', route => route.fulfill({ status: 409, contentType: 'application/json', body: JSON.stringify({ detail: 'legacy raw refusal' }) }));
  await page.getByRole('combobox', { name: 'Owner', exact: true }).click();
  await page.getByRole('option', { name: 'You', exact: true }).click();
  await expect(page.getByText('Taking ownership isn’t available when more than one person uses this CORSIA. Choose an agent instead.')).toBeVisible();
  await page.screenshot({ path: `${out}/refusal-fallback.png` });
});
