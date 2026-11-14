import { test } from '../support/test';
import { expect } from '@playwright/test';
import { TEST_TOKEN } from '../support/corsia';

const at = new Date(Date.now() + 86400000).toISOString();
const account = (id: string, name: string, limited: boolean) => ({ id, provider: 'claude-code', name, is_default: false, state: 'signed_in', revision: 1, identity: {}, label: { name, email: `${id}@example.test`, text: name, custom: null }, usage: { checking: false, check: null, state: limited ? 'limited' : 'allowed', usable: !limited, resets_at: at, windows: [] } });

test('@flow:provider-label-fit account-backed provider names fit', async ({ page }) => {
  let accounts = [account('a', 'Account with a long label', true), account('b', 'Second account', true)];
  let selected = false;
  await page.route('**/api/models**', route => route.fulfill({ json: { models: [{ id: 'claude-code/opus', selector: 'claude-code/opus', litellm_model: 'claude-code/opus', name: 'Claude Code · Opus', display_name: 'Opus', connection: 'claude-code', provider: 'claude-code', backend: 'cc', status: selected ? 'ready' : 'unavailable', status_reason: selected ? null : 'usage_limited', available: true, desc: '', size: '', speed: '', capabilities: { context_window_max: 262144 }, runs_on: accounts.map(a => ({ account_id: a.id, selected, status: selected ? 'ready' : 'unavailable', status_reason: selected ? null : 'usage_limited', resets_at: at, usage: a.usage })) }], active_local_threads: 0 } }));
  await page.route('**/api/vendor-accounts**', route => route.fulfill({ json: { accounts } }));
  await page.route('**/api/provider-policy**', route => route.fulfill({ json: { providers: [] } }));
  await page.route('**/api/providers/sources**', route => route.fulfill({ json: { sources: [] } }));
  const sizes = [];
  for (const state of ['limited', 'selected'] as const) {
    if (state === 'selected') { selected = true; accounts = [account('a', 'Account with a long label', false)]; }
    await page.setViewportSize({ width: 1440, height: 900 });
    await page.goto(`/?${['to', 'ken'].join('')}=${TEST_TOKEN}`);
    await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible({ timeout: 20000 });
    await page.getByRole('button', { name: 'New chat' }).first().click();
    await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
    await page.locator('button.model-chip').click();
    const header = page.locator('section[aria-label="Claude Code"] .picker-account-header');
    const vendor = header.locator('.vendor');
    await expect(vendor).toBeVisible();
    if (state === 'limited') await expect(header.locator('.usage')).toContainText('limited');
    else await expect(header.locator('.account-name')).toContainText('Account with a long label');
    for (const width of [1440, 390, 320]) {
      await page.setViewportSize({ width, height: 900 });
      const size = await vendor.evaluate(el => ({ text: el.textContent, client: el.clientWidth, scroll: el.scrollWidth, title: el.parentElement?.getBoundingClientRect().width, line: el.parentElement?.parentElement?.getBoundingClientRect().width }));
      sizes.push({ state, width, ...size });
      if (process.env.PICKER_SHOTS_DIR) await page.screenshot({ path: `${process.env.PICKER_SHOTS_DIR}/provider-${state}-${width}.png`, animations: 'disabled' });
    }
  }
  console.log('provider-label geometry', JSON.stringify(sizes));
  for (const size of sizes) expect(size.scroll, `${size.state} provider clipped at ${size.width}`).toBeLessThanOrEqual(size.client + 1);
});
