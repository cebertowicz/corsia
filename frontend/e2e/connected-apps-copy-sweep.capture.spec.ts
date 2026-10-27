import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, type Page } from '@playwright/test';
import { test } from './support/test';
import { loginByUrl, openSettings, openSettingsSection } from './support/corsia';

const all = { mode: 'all', project_ids: [], outside_projects: false };
const notice = "It can do anything you can do on this computer, except open CORSIA's own files and saved keys. Connect only apps you trust.";
const google = { id: 'google-work', provider: 'google', provider_display_name: 'Google', address: 'sam.work@example.com', capabilities: ['mail'], state: 'connected', reach: all };
const app = { id: 'linear', name: 'Linear', state: 'needs_sign_in', revision: 1, tool_count: 0, reach: all, transport: 'http', sign_in: { status: 'idle', signed_in: false } };
const out = '.qa/copy-sweep';

async function shot(page: Page, name: string) {
  mkdirSync(out, { recursive: true });
  await page.waitForTimeout(400);
  await page.screenshot({ path: join(out, `${name}.png`) });
}

test.describe('Connected apps copy sweep frames', () => {
  test.skip(process.env.CORSIA_CAPTURE_COPY_SWEEP !== '1', 'Capture only');
  test.setTimeout(120_000);
  test('captures each changed state', async ({ page }, info) => {
    const mobile = info.project.name === 'chromium-mobile';
    const width = mobile ? 393 : 1920;
    await page.setViewportSize({ width, height: mobile ? 852 : 1080 });
    let servers: object[] = [];
    let accounts: object[] = [];
    let signInHere = false;
    await page.route('**/api/mcp/servers**', async route => {
      const url = new URL(route.request().url());
      if (url.pathname.endsWith('/sign-in') && route.request().method() === 'POST') return route.fulfill({ json: { authorization_url: 'https://example.com/signin', expires_at: '2099-01-01T00:00:00Z', reopened: false } });
      if (route.request().method() !== 'GET') return route.fulfill({ status: 500, json: { detail: 'Could not save. Try again.' } });
      return route.fulfill({ json: { servers, accounts, sign_in_here: signInHere } });
    });
    await page.route('**/api/projects**', route => route.fulfill({ json: { projects: [] } }));
    await page.emulateMedia({ colorScheme: 'light' });
    await loginByUrl(page, { waitForComposer: false });
    const open = async () => { await openSettings(page); await openSettingsSection(page, 'Connected apps'); };
    const reload = async () => { await page.reload(); await open(); };
    await open();
    await expect(page.getByText('No connected apps. Connect one here, or ask CORSIA in chat to bring over the apps you use in Claude Code, Cursor or another tool.')).toBeVisible();
    await shot(page, `A-empty-${width}-light`);
    servers = [app];
    await reload();
    await expect(page.getByText('You can only sign in on the computer CORSIA runs on. Open CORSIA there and press Sign in.')).toBeVisible();
    await shot(page, `B-sign-in-here-${width}-light`);
    signInHere = true;
    await page.addInitScript(() => { window.open = () => null; });
    await reload();
    await page.getByRole('button', { name: 'Sign in', exact: true }).click();
    await expect(page.getByText("Your browser didn't open. Copy the sign-in link and open it in a browser on this computer.")).toBeVisible();
    await shot(page, `C-browser-${width}-light`);
    servers = [{ ...app, sign_in: { status: 'failed', signed_in: false, failure_class: 'address_mismatch' } }];
    await reload();
    await expect(page.getByText("Linear didn't recognize CORSIA, so it needs a client ID. Ask whoever looks after Linear for one, or make one in Linear's settings, then give it to CORSIA in chat.")).toBeVisible();
    await shot(page, `D-client-id-${width}-light`);
    servers = []; accounts = [google];
    await reload();
    await page.getByRole('button', { name: 'Google — sam.work@example.com', exact: true }).click();
    await page.getByRole('button', { name: 'Sign out', exact: true }).click();
    await expect(page.getByText('Agents will no longer be able to use this account, including in chats already running. You can sign in again any time.')).toBeVisible();
    await shot(page, `E-account-sign-out-${width}-light`);
    await page.getByRole('button', { name: 'Cancel' }).click();
    await page.getByRole('button', { name: 'Remove', exact: true }).click();
    await expect(page.getByText('Agents will no longer be able to use this account, including in chats already running. This signs CORSIA out and revokes CORSIA’s access to this Google account.')).toBeVisible();
    await shot(page, `E-google-remove-${width}-light`);
    accounts = []; servers = [{ id: 'local', name: 'Local app', state: 'awaiting_approval', revision: 1, reach: all, transport: 'stdio', notice }];
    await reload();
    await page.getByRole('button', { name: 'Approve', exact: true }).click();
    await expect(page.getByText(notice)).toBeVisible();
    await shot(page, `F-local-notice-${width}-light`);
    await page.getByRole('button', { name: 'Approve', exact: true }).click();
    const error = page.getByText('Could not save. Try again.');
    await expect(error).toBeVisible();
    await error.scrollIntoViewIfNeeded();
    await expect(error).toHaveClass(/error/);
    await shot(page, `G-error-${width}-light`);
    await page.emulateMedia({ colorScheme: 'dark' });
    await shot(page, `G-error-${width}-dark`);
  });
});
