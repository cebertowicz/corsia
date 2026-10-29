import { test, expect } from '../support/test';
import { TEST_TOKEN, TEST_SESSION_TOKEN } from '../support/corsia';

test('unchecked Claude sign-in recovers to ready on the original account', async ({ page, request }) => {
  test.skip(!process.env.CORSIA_E2E_RECOVERY_CLI, 'Requires the isolated recovery CLI fixture');
  test.setTimeout(120_000);
  // Only first-run scaffolding is simulated; source, account and sign-in APIs are real.
  await page.route('**/api/auth/status', route => route.fulfill({ json: { has_users: false } }));
  await page.route('**/api/setup/status', route => route.fulfill({ json: { apple_silicon: true, macos_supported: true, local_eligible: false, work_folder: '/tmp/e2e', free_disk_gb: 500 } }));
  await page.route('**/api/setup/first-account', route => route.fulfill({ json: { token: TEST_SESSION_TOKEN, user: { id: 'e2e', name: 'E2E' } } }));
  await page.route('**/api/setup/progress', route => route.fulfill({ json: { setup_completed: false, step: 'models' } }));
  await page.goto('/');
  await page.getByRole('button', { name: 'Continue', exact: true }).click();
  await page.getByLabel('Full name', { exact: true }).fill('E2E Person');
  await page.getByLabel('Password', { exact: true }).fill('e2e-password');
  await page.getByLabel('Confirm password', { exact: true }).fill('e2e-password');
  await page.getByRole('button', { name: 'Continue', exact: true }).click();
  await expect(page.getByRole('heading', { name: /^(Your subscriptions|Providers & models)$/ })).toBeVisible();
  if (await page.getByRole('heading', { name: 'Your subscriptions' }).isVisible()) await page.getByRole('button', { name: 'Claude', exact: true }).click();
  await expect(page.getByRole('heading', { name: 'Providers & models' })).toBeVisible();
  const claude = page.getByRole('region', { name: 'Claude Code' });
  await claude.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(claude).toContainText('Couldn’t check your Claude models. Sign in again.', { timeout: 45000 });
  const sources = async () => {
    const response = await request.get('/api/providers/sources', { params: { token: TEST_TOKEN } });
    expect(response.ok()).toBeTruthy();
    const body = await response.json();
    return body.sources.find((source: { source: string }) => source.source === 'claude-code');
  };
  const first = await sources();
  expect(first.accounts.filter((account: { state: string }) => account.state === 'unchecked')).toHaveLength(1);
  const id = first.accounts.find((account: { state: string }) => account.state === 'unchecked').account_id;
  await claude.getByRole('button', { name: 'Sign in', exact: true }).click();
  await expect(claude).toContainText('Ready', { timeout: 45000 });
  const final = await sources();
  expect(final.accounts.filter((account: { state: string }) => account.state === 'ready')).toHaveLength(1);
  expect(final.accounts.find((account: { state: string }) => account.state === 'ready').account_id).toBe(id);
  const accountsResponse = await request.get('/api/vendor-accounts', { params: { token: TEST_TOKEN, provider: 'claude-code' } });
  expect(accountsResponse.ok()).toBeTruthy();
  const accounts = (await accountsResponse.json()).accounts.filter((account: { is_default: boolean }) => !account.is_default);
  expect(accounts).toHaveLength(2);
  const signedIn = accounts.filter((account: { state: string }) => account.state === 'signed_in');
  const signedOut = accounts.filter((account: { state: string }) => account.state === 'signed_out');
  expect(signedIn).toHaveLength(1);
  expect(signedIn[0].id).toBe(id);
  // Issue #1702 tracks removing the signed-out retry row; update this assertion when fixed.
  expect(signedOut).toHaveLength(1);
  expect(signedOut[0].id).not.toBe(id);
});
