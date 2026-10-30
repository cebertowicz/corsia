import { test, expect } from '../support/test';
import { TEST_SESSION_TOKEN } from '../support/corsia';
import type { Page } from '@playwright/test';

async function settledSetup(page: Page) {
 await expect(page.locator('.login-screen')).toHaveCount(0);
 await expect(page.locator('[data-setup-page]:not([data-setup-page="models"])')).toHaveCount(0);
 await page.locator('[data-setup-page="models"]').evaluate(async (scene) => {
  await Promise.all(scene.getAnimations({ subtree: true }).filter((animation) => animation.effect?.getComputedTiming().iterations !== Infinity).map((animation) => animation.finished.catch(() => undefined)));
 });
}

test('@surface:first-run restart offers retry for the downloaded model without a refusal line', async ({ page }, testInfo) => {
 await page.route('**/api/setup/progress', route => route.fulfill({ json: { setup_completed: false, status: { local_started: true, local_eligible: true, macos_supported: true } } }));
 await page.route('**/api/providers/sources**', route => route.fulfill({ json: { sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup', local_state: 'stopped' }] } }));
 await page.route('**/api/providers/local/setup', route => route.fulfill({ json: { state: 'start_failed', model: 'owner/local-model' } }));
 await page.route('**/api/models?thread_id=**', route => route.fulfill({ json: { models: [] } }));
 await page.route('**/api/settings/providers**', route => route.fulfill({ json: { providers: [{ name: 'omlx', type: 'network' }] } }));
 await page.route('**/api/settings/network-servers**', route => {
  const url = route.request().url();
  if (url.includes('/models/load')) return route.fulfill({ status: 500, json: { detail: 'Load failed' } });
  return route.fulfill({ json: url.includes('/models') ? { loaded: [], available: [{ id: 'local-model', pinned: true, starting: false }] } : { servers: [{ id: 'own', provider: 'omlx', corsia_omlx: true }] } });
 });
 await page.goto('/?token=' + encodeURIComponent(TEST_SESSION_TOKEN));
 await expect(page.locator('[data-setup-page="models"]')).toBeVisible();
 await expect(page.getByRole('button', { name: 'Retry' })).toBeVisible();
 await expect(page.getByText('Downloaded', { exact: true })).toBeVisible();
 await expect(page.getByRole('button', { name: 'Download' })).toHaveCount(0);
 await settledSetup(page);
 await page.screenshot({ path: testInfo.outputPath('restart-retry.png') });
 await page.getByRole('button', { name: 'Retry' }).click();
 await expect(page.getByText('Load failed', { exact: true })).toBeVisible();
});

test('@surface:first-run retry resumes a partial download even when listed as available', async ({ page }) => {
 let loads = 0;
 page.on('request', request => { if (request.url().includes('/models/load')) loads++; });
 await page.route('**/api/setup/progress', route => route.fulfill({ json: { setup_completed: false, status: { local_started: true, local_eligible: true, macos_supported: true } } }));
 await page.route('**/api/providers/sources**', route => route.fulfill({ json: { sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup', local_state: 'failed', reason_code: 'not_running' }] } }));
 await page.route('**/api/providers/local/setup', route => route.fulfill({ json: { state: 'failed', model: 'owner/local-model' } }));
 await page.route('**/api/models?thread_id=**', route => route.fulfill({ json: { models: [] } }));
 await page.route('**/api/settings/providers**', route => route.fulfill({ json: { providers: [{ name: 'omlx', type: 'network' }] } }));
 await page.route('**/api/settings/network-servers**', route => route.fulfill({ json: route.request().url().includes('/models') ? { loaded: [], available: [{ id: 'local-model', pinned: true, starting: false }] } : { servers: [{ id: 'own', provider: 'omlx', corsia_omlx: true }] } }));
 await page.goto('/?token=' + encodeURIComponent(TEST_SESSION_TOKEN));
 await expect(page.locator('[data-setup-page="models"]')).toBeVisible();
 await expect(page.getByText('Downloaded', { exact: true })).toHaveCount(0);
 const request = page.waitForRequest(req => req.url().includes('/api/providers/local/setup') && req.method() === 'POST');
 await page.getByRole('button', { name: 'Retry' }).click();
 await request;
 expect(loads).toBe(0);
});

test('@surface:first-run setup shows a local load refusal and retries', async ({ page }, testInfo) => {
 const line = 'Not loaded: needs 12 GB, 8 GB free for a model now';
 const retryError = 'Load refused by server';
 let attempts = 0;
 await page.route('**/api/providers/local/setup', route => route.fulfill({ json: { state: 'start_failed', model: 'owner/local-model' } }));
 await page.route('**/api/models?thread_id=**', route => route.fulfill({ json: { models: attempts >= 2 ? [{ id: 'local-model', name: 'local-model', connection: 'omlx', status: 'ready' }] : [] } }));
 await page.route('**/api/setup/progress', route => route.fulfill({ json: { setup_completed: false, status: { local_started: true, local_eligible: true, macos_supported: true } } }));
 await page.route('**/api/providers/sources**', route => route.fulfill({ json: { sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup', local_state: 'stopped' }] } }));
 await page.route('**/api/settings/providers**', route => route.fulfill({ json: { providers: [{ name: 'omlx', type: 'network' }] } }));
 await page.route('**/api/settings/network-servers**', route => {
  const url = route.request().url();
  if (url.includes('/models/load')) { attempts++; return attempts === 1 ? route.fulfill({ status: 500, json: { detail: retryError } }) : route.fulfill({ json: { ok: true } }); }
  if (url.includes('/models')) return route.fulfill({ json: { loaded: [], available: [{ id: 'local-model', pinned: true, starting: false, keep_line: attempts ? null : line }] } });
  return route.fulfill({ json: { servers: [{ id: 'own', provider: 'omlx', corsia_omlx: true }] } });
 });
 await page.goto(`/?token=${TEST_SESSION_TOKEN}`);
 await expect(page.locator('[data-setup-page="models"]')).toBeVisible();
 await expect(page.getByText(line, { exact: true })).toBeVisible();
 await settledSetup(page);
 await page.screenshot({ path: testInfo.outputPath('refusal.png') });
 const request = page.waitForRequest(req => req.url().includes('/models/load') && req.method() === 'POST');
 await page.getByRole('button', { name: 'Retry' }).click();
 expect((await request).postDataJSON()).toEqual({ model: 'local-model' });
 await expect(page.getByText(line, { exact: true })).toBeVisible();
 await expect(page.getByText(retryError, { exact: true })).toBeVisible();
 await settledSetup(page);
 await page.screenshot({ path: testInfo.outputPath('retry-refused.png') });
 await page.getByRole('button', { name: 'Retry' }).click();
 await expect(page.getByRole('button', { name: 'Start using CORSIA' })).toBeEnabled();
 await expect(page.getByText(line, { exact: true })).toHaveCount(0);
 await settledSetup(page);
 await page.screenshot({ path: testInfo.outputPath('retry-succeeded.png') });
});

test('@surface:first-run a ready model takes precedence over a different refusal', async ({ page }, testInfo) => {
 const line = 'Not loaded: another model cannot fit';
 await page.route('**/api/setup/progress', route => route.fulfill({ json: { setup_completed: false, status: { local_started: true, local_eligible: true, macos_supported: true } } }));
 await page.route('**/api/providers/sources**', route => route.fulfill({ json: { sources: [{ source: 'omlx', name: 'Local model', state: 'ready' }] } }));
 await page.route('**/api/providers/local/setup', route => route.fulfill({ json: { model: 'owner/setup-model' } }));
 await page.route('**/api/models?thread_id=**', route => route.fulfill({ json: { models: [{ id: 'ready-model', name: 'ready-model', connection: 'omlx', status: 'ready' }] } }));
 await page.route('**/api/settings/providers**', route => route.fulfill({ json: { providers: [{ name: 'omlx', type: 'network' }] } }));
 await page.route('**/api/settings/network-servers**', route => route.fulfill({ json: route.request().url().includes('/models') ? { loaded: [{ id: 'ready-model' }], available: [{ id: 'other-model', pinned: true, keep_line: line }] } : { servers: [{ id: 'own', provider: 'omlx', corsia_omlx: true }] } }));
 await page.goto(`/?token=${TEST_SESSION_TOKEN}`);
 await expect(page.locator('[data-setup-page="models"]')).toBeVisible();
 await expect(page.getByRole('button', { name: 'Start using CORSIA' })).toBeEnabled();
 await expect(page.getByText(line, { exact: true })).toHaveCount(0);
 await settledSetup(page);
 await page.screenshot({ path: testInfo.outputPath('ready-with-other-refusal.png') });
});
