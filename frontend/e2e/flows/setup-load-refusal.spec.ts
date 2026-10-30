import { test, expect } from '../support/test';
import { TEST_SESSION_TOKEN } from '../support/corsia';

test('@surface:first-run setup shows a local load refusal and retries', async ({ page }) => {
 const line = 'Not loaded: needs 12 GB, 8 GB free for a model now';
 await page.route('**/api/setup/progress', route => route.fulfill({ json: { setup_completed: false, status: { local_started: true, local_eligible: true, macos_supported: true } } }));
 await page.route('**/api/providers/sources**', route => route.fulfill({ json: { sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup', local_state: 'stopped' }] } }));
 await page.route('**/api/settings/providers**', route => route.fulfill({ json: { providers: [{ name: 'omlx', type: 'network' }] } }));
 await page.route('**/api/settings/network-servers**', route => {
  const url = route.request().url();
  if (url.includes('/models/load')) return route.fulfill({ status: 500, json: { detail: 'refused' } });
  if (url.includes('/models')) return route.fulfill({ json: { loaded: [], available: [{ id: 'local-model', pinned: true, starting: false, keep_line: line }] } });
  return route.fulfill({ json: { servers: [{ id: 'own', provider: 'omlx', corsia_omlx: true }] } });
 });
 await page.goto(`/?token=${TEST_SESSION_TOKEN}`);
 await expect(page.locator('[data-setup-page="models"]')).toBeVisible();
 await expect(page.getByText(line, { exact: true })).toBeVisible();
 const request = page.waitForRequest(req => req.url().includes('/models/load') && req.method() === 'POST');
 await page.getByRole('button', { name: 'Retry' }).click();
 expect((await request).postDataJSON()).toEqual({ model: 'local-model' });
 await expect(page.getByText(line, { exact: true })).toBeVisible();
});
