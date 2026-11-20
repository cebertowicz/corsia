import { mkdirSync } from 'node:fs';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { isolatedDb, setDebugVersionOverlay } from '../support/heldComments';
import { ownerItem, rolePicker, seedOwnedDocument } from '../support/documentOwnerSupervisor';

for (const [label, width, height] of [['wide', 1440, 900], ['phone', 393, 852]] as const) {
 for (const theme of ['light', 'dark'] as const) {
  test(`person ownership frames ${label} ${theme}`, async ({ page, request }, info) => {
   test.skip(info.project.name !== (label === 'wide' ? 'chromium-desktop' : 'chromium-mobile'));
   test.setTimeout(120_000);
   await page.setViewportSize({ width, height });
   await page.addInitScript(value => localStorage.setItem('corsia_theme', value), theme);
   await setDebugVersionOverlay(request, false);
   const seeded = await seedOwnedDocument(page, request);
   isolatedDb(["connection.execute(\"INSERT OR IGNORE INTO users (id,password_hash,created_at,password_changed_at) VALUES ('second-person','x','2026-01-01','2026-01-01')\")"]);
   await seeded.open(page);
   await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
   await expect(ownerItem(page)).toHaveAccessibleName('Owner: Release planning');
   const proof = process.env.CORSIA_PERSON_OWNER_FRAMES;
   if (proof) { mkdirSync(proof, { recursive: true }); await page.screenshot({ path: `${proof}/${label}-${theme}-before.png` }); }
   await ownerItem(page).click();
   await seeded.option(rolePicker(page), 'You').click();
   await expect(ownerItem(page)).toHaveAccessibleName('Owner: You');
   if (proof) await page.screenshot({ path: `${proof}/${label}-${theme}-after.png` });
   await page.reload();
   if (width < 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
   await page.getByText('Release planning', { exact: true }).first().click();
   await expect(ownerItem(page)).toHaveAccessibleName('Owner: You');
  });
 }
}
