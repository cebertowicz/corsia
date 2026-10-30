import { test, expect } from '../support/test';
import { TEST_SESSION_TOKEN } from '../support/corsia';

test('@surface:first-run shows the refusal, reference and existing Retry', async ({ page }) => {
 const line = 'Not loaded: needs 12 GB, 8 GB free for a model now';
 const fix = 'CORSIA couldn’t load the model because there isn’t enough free memory. Close other apps, then press Retry.';
 let retried = false;
 await page.route('**/api/setup/progress', route => route.fulfill({ json: { setup_completed: false, status: { local_started: true, local_eligible: true, macos_supported: true } } }));
 await page.route('**/api/providers/sources**', route => route.fulfill({ json: { sources: [{ source: 'omlx', name: 'Local model', state: 'ready', local_state: retried ? 'stopped' : 'start_failed', setup_error_code: 'load_refused', setup_error_reference: 'local-42', fix, detail: line }] } }));
 await page.route('**/api/models?thread_id=**', route => route.fulfill({ json: { models: retried ? [{ id: 'local-model', name: 'local-model', connection: 'omlx', status: 'ready' }] : [] } }));
 await page.route('**/api/settings/providers**', route => route.fulfill({ json: { providers: [{ name: 'omlx', type: 'network' }] } }));
 await page.route('**/api/providers/local/setup', route => {
  if (route.request().method() === 'POST') { retried = true; return route.fulfill({ json: { ok: true, in_progress: true } }); }
  return route.fulfill({ json: { state: 'start_failed', model: 'owner/local-model' } });
 });
 await page.goto('/?token=' + encodeURIComponent(TEST_SESSION_TOKEN));
 await expect(page.locator('[data-setup-page="models"]')).toBeVisible();
 await expect(page.getByText(line, { exact: true })).toBeVisible();
 await expect(page.getByText(fix, { exact: true })).toBeVisible();
 await expect(page.getByText(/local-42/)).toBeVisible();
 await expect(page.getByRole('button', { name: 'Download', exact: true })).toHaveCount(0);
 const request = page.waitForRequest(req => req.url().includes('/api/providers/local/setup') && req.method() === 'POST');
 await page.getByRole('button', { name: 'Retry' }).click();
 await request;
 await expect(page.getByRole('button', { name: 'Start using CORSIA' })).toBeEnabled();
});
