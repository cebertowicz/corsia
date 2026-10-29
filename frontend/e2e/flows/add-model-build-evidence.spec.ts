import { test } from '../support/test';
import { expect, webkit, type Page } from '@playwright/test';
import { loginByUrl, openSettings, openSettingsSection } from '../support/corsia';

test.setTimeout(180_000);
const shots = process.env.ADD_MODEL_BUILD_SHOTS_DIR;
const server = { id: 'omlx-local', provider: 'omlx', instance_id: 'this-mac', machine_id: 'this-mac', instance_name: 'This Mac', kind: 'managed', port: 11434, status: 'online', source: 'manual', latency_ms: 1, memory_total_mb: 262144, memory_used_mb: 0, gpu_info: null, endpoints: [{ id: 'endpoint', server_id: 'omlx-local', host: '127.0.0.1', kind: 'local', last_ok_at: null, last_latency_ms: 1 }], active_host: '127.0.0.1', api_base: null, mgmt_base: null, corsia_omlx: true };
const provider = { name: 'omlx', label: 'oMLX', type: 'network', has_key: false, masked_key: null, api_base: null, docs_url: null, models: [], enabled: true };
const builds = [
  { name: 'Qwen3.8 Flash Next', repository: 'org/Qwen3.8-Flash-Next-oQ4e-mtp', download_size_bytes: 104e9, download_size_gb: 104, context_window: 262144, quantization_bits: 4, smarts_level: 5, tokens_per_second: 63, fits_now: true, disk: { state: 'ready' }, section: 'best' },
  { name: 'Qwen3.8 Flash Next', repository: 'org/Qwen3.8-Flash-Next-oQ6e', download_size_bytes: 156e9, download_size_gb: 156, context_window: 262144, quantization_bits: 6, smarts_level: 5, tokens_per_second: 42, fits_now: true, disk: { state: 'ready' }, section: 'best' },
  { name: 'Gemma', repository: 'org/Gemma-oQ4e', download_size_bytes: 20e9, download_size_gb: 20, context_window: 131072, quantization_bits: 4, smarts_level: 4, tokens_per_second: 90, fits_now: true, disk: { state: 'ready' }, section: 'faster' },
];
type Scenario = 'loaded' | 'owned' | 'available' | 'downloading' | 'failed' | 'small';
async function fixture(page: Page, state: Scenario) {
  const running = state === 'downloading' || state === 'failed';
  let download = running ? { repository: builds[0].repository, name: builds[0].name, percent: 40, failed: state === 'failed', downloaded: false } : null;
  const models = builds.map((build, i) => ({ ...build, fits_now: state === 'small' ? i === 2 : build.fits_now, on_disk: state === 'owned' ? i < 2 : state === 'loaded' && i === 0 }));
  await page.route('**/api/settings/providers?**', route => route.fulfill({ json: { providers: [provider] } }));
  await page.route('**/api/settings/network-servers?**', route => route.fulfill({ json: { servers: [server] } }));
  await page.route('**/api/settings/network-servers/omlx-local/models?**', route => route.fulfill({ json: { loaded: state === 'loaded' ? [{ id: 'Qwen3.8-Flash-Next-oQ4e-mtp', model_id: 'Qwen3.8-Flash-Next-oQ4e-mtp', repo_id: builds[0].repository, display_name: builds[0].name, context_length: 262144, size_bytes: 104 * 1024 ** 3 }] : [], available: [] } }));
  await page.route('**/api/settings/network-servers/omlx-local/jobs/active?**', route => route.fulfill({ json: { jobs: [] } }));
  await page.route('**/api/system/host-resources?**', route => route.fulfill({ json: { device_name: 'This Mac', total_gb: state === 'small' ? 36 : 256, free_gb: state === 'small' ? 30 : 120, models_used_gb: state === 'loaded' ? 104 : 0, other_used_gb: 0, safe_free_gb: 100, safe_reserve_gb: 16, by_provider: {} } }));
  await page.route('**/api/providers/sources', route => route.fulfill({ json: { sources: [{ source: 'omlx', name: 'Local model', state: 'ready', eligible: true, corsia_omlx_online: true, settings_download: download }] } }));
  await page.route('**/api/providers/local/recommendations', route => route.fulfill({ json: { state: 'ready', memory_gb: state === 'small' ? 36 : 256, memory_in_use_gb: state === 'loaded' ? 104 : 0, memory_room_gb: state === 'small' ? 31.5 : 240, disk_free_gb: 200, models, downloads: [], recommended_repository: state === 'small' ? null : builds[0].repository } }));
  await page.route('**/api/providers/local/downloads**', async route => {
    if (route.request().method() === 'POST') {
      const repository = route.request().postDataJSON().repository as string;
      const build = builds.find(item => item.repository === repository)!;
      download = { repository, name: build.name, percent: 40, failed: false, downloaded: false };
      await route.fulfill({ json: { ok: true } });
    } else if (route.request().method() === 'DELETE') {
      download = null;
      await route.fulfill({ json: { ok: true } });
    } else await route.fulfill({ json: [] });
  });
  await page.route('**/api/providers/local/cancel', async route => {
    download = null;
    await route.fulfill({ json: { ok: true, cancelled: true } });
  });
  await page.route('**/api/vendor-accounts**', route => route.fulfill({ json: { accounts: [] } }));
  await page.route('**/api/models**', route => route.fulfill({ json: { models: [], active_local_threads: 0 } }));
}

test('Add model WebKit keyboard actions', async ({}, info) => {
  test.skip(process.env.CORSIA_E2E_WEBKIT !== '1' || info.project.name !== 'chromium-desktop');
  const browser = await webkit.launch();
  const context = await browser.newContext({ baseURL: `http://127.0.0.1:${process.env.CORSIA_E2E_FRONTEND_PORT ?? '5178'}` });
  const page = await context.newPage();
  try {
    for (const viewport of [{ name: '1440', width: 1440, height: 900 }, { name: '393', width: 393, height: 852 }]) {
      await page.setViewportSize(viewport);
      for (const state of ['available', 'downloading'] as const) {
        await page.unrouteAll({ behavior: 'ignoreErrors' });
        await fixture(page, state);
        await loginByUrl(page);
        await openSettings(page);
        await openSettingsSection(page, 'Providers');
        const card = page.locator('[data-server-card="omlx-local"]');
        await card.locator('.pr-expand').click();
        await card.getByRole('button', { name: 'Add model', exact: true }).click();
        const sheet = page.locator('.modal').filter({ hasText: 'Add model' });
        const actions = state === 'available' ? sheet.getByRole('button', { name: /^Download / }) : sheet.getByRole('button', { name: /^Cancel download/ });
        const count = await actions.count();
        expect(count).toBe(state === 'available' ? 3 : 1);
        const seen = new Set<string>();
        for (let i = 0; i < 30 && seen.size < count; i++) {
          await page.keyboard.press('Tab');
          const focused = page.locator(':focus');
          const label = await focused.getAttribute('aria-label').catch(() => null);
          if (label && (label.startsWith('Download ') || label.startsWith('Cancel download'))) {
            seen.add(label);
            const style = await focused.evaluate((element) => ({ outline: getComputedStyle(element).outlineStyle, shadow: getComputedStyle(element).boxShadow }));
            expect(style.outline !== 'none' || style.shadow !== 'none').toBeTruthy();
            if (shots) await page.screenshot({ path: `${shots}/webkit-focus-${state}-${viewport.name}.png`, fullPage: true, animations: 'disabled', scale: 'css' });
          }
        }
        expect(seen.size).toBe(count);
      }
    }
  } finally {
    await context.close();
    await browser.close();
  }
});

test('Add model download and cancel journey', async ({ page }, info) => {
  test.skip(info.project.name !== 'chromium-desktop');
  await fixture(page, 'available');
  await loginByUrl(page);
  await openSettings(page);
  await openSettingsSection(page, 'Providers');
  const card = page.locator('[data-server-card="omlx-local"]');
  await card.locator('.pr-expand').click();
  await card.getByRole('button', { name: 'Add model', exact: true }).click();
  const sheet = page.locator('.modal').filter({ hasText: 'Add model' });
  await sheet.getByRole('button', { name: /Download Qwen3.8 Flash Next oQ4e-mtp/ }).click();
  // The empty Settings state is replaced by the download row, which closes its sheet.
  await expect(sheet).toBeHidden();
  await expect(card).toContainText('Downloading 40%');
  await card.getByRole('button', { name: 'Add model', exact: true }).click();
  await expect(sheet).toContainText('Downloading 40%');
  await sheet.getByRole('button', { name: /Cancel download of Qwen3.8 Flash Next oQ4e-mtp/ }).click();
  await expect(sheet.getByRole('button', { name: /Download Qwen3.8 Flash Next oQ4e-mtp/ })).toBeEnabled();
});

test('Add model small Mac list', async ({ page }, info) => {
  test.skip(info.project.name !== 'chromium-desktop');
  test.setTimeout(120_000);
  for (const viewport of [{ name: '1440', width: 1440, height: 900 }, { name: '393', width: 393, height: 852 }]) {
    for (const theme of ['light', 'dark'] as const) {
      await page.unrouteAll({ behavior: 'ignoreErrors' });
      await page.setViewportSize(viewport);
      await page.emulateMedia({ colorScheme: theme });
      await fixture(page, 'small');
      await loginByUrl(page);
      await openSettings(page);
      await openSettingsSection(page, 'Providers');
      const card = page.locator('[data-server-card="omlx-local"]');
      await card.locator('.pr-expand').click();
      await card.getByRole('button', { name: 'Add model', exact: true }).click();
      const sheet = page.locator('.modal').filter({ hasText: 'Add model' });
      await expect(sheet).toContainText('36 GB memory');
      await expect(sheet).toContainText('Gemma');
      await expect(sheet).not.toContainText('Qwen3.8 Flash Next');
      if (shots) await page.screenshot({ path: `${shots}/small-${viewport.name}-${theme}.png`, fullPage: true, animations: 'disabled', scale: 'css' });
    }
  }
});

test('Add model approved build states', async ({ page }, info) => {
  test.skip(info.project.name !== 'chromium-desktop');
  test.setTimeout(180_000);
  for (const viewport of [{ name: '1440', width: 1440, height: 900 }, { name: '393', width: 393, height: 852 }]) {
    for (const theme of ['light', 'dark'] as const) {
      for (const state of ['loaded', 'owned', 'available', 'downloading', 'failed'] as const) {
        await page.unrouteAll({ behavior: 'ignoreErrors' });
        await page.setViewportSize(viewport);
        await page.emulateMedia({ colorScheme: theme });
        await fixture(page, state);
        await loginByUrl(page);
        await openSettings(page);
        await openSettingsSection(page, 'Providers');
        const card = page.locator('[data-server-card="omlx-local"]');
        await card.locator('.pr-expand').click();
        if (state === 'downloading') {
          await expect(card).toContainText('Downloading 40%');
          if (shots) await page.screenshot({ path: `${shots}/settings-downloading-${viewport.name}-${theme}.png`, fullPage: true, animations: 'disabled', scale: 'css' });
        }
        await card.getByRole('button', { name: 'Add model', exact: true }).click();
        const sheet = page.locator('.modal').filter({ hasText: 'Add model' });
        await expect(sheet).toContainText('Recommended');
        if (state === 'owned' || state === 'loaded') await expect(sheet).toContainText('on this computer');
        if (state === 'downloading') await expect(sheet).toContainText('Downloading 40%');
        if (state === 'failed') await expect(sheet.getByRole('button', { name: /Retry download/ })).toBeVisible();
        if (shots) await page.screenshot({ path: `${shots}/${state}-${viewport.name}-${theme}.png`, fullPage: true, animations: 'disabled', scale: 'css' });
      }
    }
  }
});
