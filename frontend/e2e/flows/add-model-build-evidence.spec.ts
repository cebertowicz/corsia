import { test } from '../support/test';
import { expect, webkit, type Page, type WebSocketRoute } from '@playwright/test';
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
type Download = { repository: string; name: string; percent: number; failed: boolean; downloaded: boolean } | null;
async function fixture(page: Page, state: Scenario) {
  const running = state === 'downloading' || state === 'failed';
  let download: Download = running ? { repository: builds[0].repository, name: builds[0].name, percent: 40, failed: state === 'failed', downloaded: false } : null;
  const calls: string[] = [];
  const models = builds.map((build, i) => ({ ...build, fits_now: state === 'small' ? i === 2 : build.fits_now, on_disk: state === 'owned' ? i < 2 : state === 'loaded' && i === 0 }));
  await page.route('**/api/settings/providers?**', route => route.fulfill({ json: { providers: [provider] } }));
  await page.route('**/api/settings/network-servers?**', route => route.fulfill({ json: { servers: [server] } }));
  await page.route('**/api/settings/network-servers/omlx-local/models?**', route => route.fulfill({ json: { loaded: state === 'loaded' ? [{ id: 'Qwen3.8-Flash-Next-oQ4e-mtp', model_id: 'Qwen3.8-Flash-Next-oQ4e-mtp', repo_id: builds[0].repository, display_name: builds[0].name, context_length: 262144, size_bytes: 104 * 1024 ** 3 }] : [], available: [] } }));
  await page.route('**/api/settings/network-servers/omlx-local/jobs/active?**', route => route.fulfill({ json: { jobs: [] } }));
  await page.route('**/api/system/host-resources?**', route => route.fulfill({ json: { device_name: 'This Mac', total_gb: state === 'small' ? 36 : 256, free_gb: state === 'small' ? 30 : 120, models_used_gb: state === 'loaded' ? 104 : 0, other_used_gb: 0, safe_free_gb: 100, safe_reserve_gb: 16, by_provider: {} } }));
  await page.route('**/api/providers/sources', route => route.fulfill({ json: { sources: [{ source: 'omlx', name: 'Local model', state: 'ready', eligible: true, corsia_omlx_online: true, settings_download: download }] } }));
  await page.route('**/api/providers/local/recommendations', route => route.fulfill({ json: { state: 'ready', memory_gb: state === 'small' ? 36 : 256, memory_in_use_gb: state === 'loaded' ? 104 : 0, memory_room_gb: state === 'small' ? 31.5 : 240, disk_free_gb: 200, models, downloads: [], recommended_repository: state === 'small' ? null : builds[0].repository } }));
  await page.route('**/api/providers/local/downloads**', async route => {
    if (route.request().method() !== 'GET') calls.push(`${route.request().method()} ${new URL(route.request().url()).pathname}`);
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
    calls.push('POST /api/providers/local/cancel');
    download = null;
    await route.fulfill({ json: { ok: true, cancelled: true } });
  });
  await page.route('**/api/vendor-accounts**', route => route.fulfill({ json: { accounts: [] } }));
  await page.route('**/api/models**', route => route.fulfill({ json: { models: [], active_local_threads: 0 } }));
  return {
    calls,
    setDownload(next: Download) { download = next; },
    finish(repository: string) { download = null; models.forEach(model => { if (model.repository === repository) model.on_disk = true; }); },
  };
}

/** Pass the live socket through, so a test can announce a download change as the backend does. */
async function liveSocket(page: Page) {
  let socket: WebSocketRoute | null = null;
  await page.routeWebSocket(url => url.pathname === '/ws', client => {
    const upstream = client.connectToServer();
    socket = client;
    client.onMessage(payload => upstream.send(payload));
    upstream.onMessage(payload => client.send(payload));
  });
  return async () => {
    await expect.poll(() => socket !== null, { timeout: 10_000 }).toBe(true);
    socket!.send(JSON.stringify({ type: 'models_updated' }));
  };
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
          // WebKit on macOS follows Safari's default: Tab moves between form fields only, and
          // Option-Tab reaches buttons too.
          await page.keyboard.press('Alt+Tab');
          const label = await page.evaluate(() => document.activeElement?.getAttribute('aria-label') ?? null);
          if (label && (label.startsWith('Download ') || label.startsWith('Cancel download'))) {
            seen.add(label);
            const style = await page.evaluate(() => {
              const element = document.activeElement as Element;
              return { outline: getComputedStyle(element).outlineStyle, shadow: getComputedStyle(element).boxShadow };
            });
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

test('Add model sheet motion and Sort press', async ({ page }, info) => {
  test.skip(info.project.name !== 'chromium-desktop');
  await fixture(page, 'available');
  await loginByUrl(page);
  await openSettings(page);
  await openSettingsSection(page, 'Providers');
  const card = page.locator('[data-server-card="omlx-local"]');
  await card.locator('.pr-expand').click();
  // Svelte runs the card's rise as a Web Animation: record it as it is created, so a slow run can't miss it.
  await page.evaluate(() => {
    const record = window as unknown as { rises: { duration: unknown; first: number }[] };
    record.rises = [];
    const animate = Element.prototype.animate;
    Element.prototype.animate = function (keyframes, options) {
      if (this.classList.contains('modal-frame') && Array.isArray(keyframes)) record.rises.push({ duration: typeof options === 'object' ? options.duration : options, first: Math.round(Number.parseFloat(String(keyframes[0]?.transform).replace('translateY(', ''))) });
      return animate.call(this, keyframes, options);
    };
  });
  const rise = async () => {
    await card.getByRole('button', { name: 'Add model', exact: true }).click();
    // Svelte first runs a zero-length animation for the delay, then the rise itself.
    await expect.poll(() => page.evaluate(() => (window as unknown as { rises: { duration: unknown }[] }).rises.filter(item => item.duration !== 0).length)).toBe(1);
    return page.evaluate(() => (window as unknown as { rises: { duration: unknown; first: number }[] }).rises.splice(0).filter(item => item.duration !== 0));
  };
  // The sheet rises 12px over Modal's shared 380 ms; with reduced motion it only fades (DESIGN-SYSTEM 2.7.5).
  expect((await rise())[0]).toEqual({ duration: 380, first: 12 });
  const sheet = page.locator('.modal').filter({ hasText: 'Add model' });
  await sheet.getByRole('button', { name: 'Close' }).click();
  await expect(sheet).toHaveCount(0);
  await page.evaluate(() => (window as unknown as { rises: unknown[] }).rises.splice(0));
  await page.emulateMedia({ reducedMotion: 'reduce' });
  expect((await rise())[0]).toEqual({ duration: 380, first: 0 });
  // Sort is a radiogroup: pressing Speed checks it and puts the fastest build first.
  const sort = sheet.getByRole('radiogroup', { name: 'Sort models' });
  await expect(sort.getByRole('radio', { name: 'Quality' })).toHaveAttribute('aria-checked', 'true');
  await sort.getByRole('radio', { name: 'Speed' }).click();
  await expect(sort.getByRole('radio', { name: 'Speed' })).toHaveAttribute('aria-checked', 'true');
  await expect(sort.getByRole('radio', { name: 'Quality' })).toHaveAttribute('aria-checked', 'false');
  await expect(sheet.locator('h3').first()).toHaveText('Gemma');
  if (shots) await page.screenshot({ path: `${shots}/sort-speed-1440-light.png`, fullPage: true, animations: 'disabled', scale: 'css' });
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
  // The empty Settings state is replaced by the download row; the sheet stays open over it.
  await expect(sheet).toContainText('Downloading 40%');
  await expect(card).toContainText('Downloading 40%');
  await sheet.getByRole('button', { name: /Cancel download of Qwen3.8 Flash Next oQ4e-mtp/ }).click();
  await expect(sheet.getByRole('button', { name: /Download Qwen3.8 Flash Next oQ4e-mtp/ })).toBeEnabled();
});

test('Add model failure, retry, delete and complete journey', async ({ page }, info) => {
  test.skip(info.project.name !== 'chromium-desktop');
  const announce = await liveSocket(page);
  const state = await fixture(page, 'failed');
  const [qwen] = builds;
  const failed = { repository: qwen.repository, name: qwen.name, percent: 40, failed: true, downloaded: false };
  await loginByUrl(page);
  await openSettings(page);
  await openSettingsSection(page, 'Providers');
  const card = page.locator('[data-server-card="omlx-local"]');
  await card.locator('.pr-expand').click();
  const sheet = page.locator('.modal').filter({ hasText: 'Add model' });
  const qwenDownload = sheet.getByRole('button', { name: /^Download Qwen3.8 Flash Next oQ4e-mtp$/ });
  const gemmaDownload = sheet.getByRole('button', { name: /^Download Gemma oQ4e$/ });
  await card.getByRole('button', { name: 'Add model', exact: true }).click();
  await expect(sheet).toContainText('Recommended');

  // The sheet stays open through every step: the Settings list behind it changes state, the sheet does not.
  // A failed download keeps Download on the other models, as dev does; deleting a partial one asks nothing.
  await expect(sheet).toContainText('Couldn’t download this model.');
  await expect(gemmaDownload).toBeEnabled();
  await sheet.getByRole('button', { name: /Delete download of Qwen3.8 Flash Next oQ4e-mtp/ }).click();
  await expect(page.locator('.modal').filter({ hasText: 'from this computer?' })).toHaveCount(0);
  await expect(qwenDownload).toBeEnabled();

  // Download again: the build shows progress and the other models wait.
  await qwenDownload.click();
  await expect(sheet).toContainText('Downloading 40%');
  await expect(gemmaDownload).toBeDisabled();

  // It fails; Retry starts it again.
  state.setDownload(failed);
  await announce();
  await expect(sheet).toContainText('Couldn’t download this model.');
  await sheet.getByRole('button', { name: /Retry download of Qwen3.8 Flash Next oQ4e-mtp/ }).click();
  await expect(sheet).toContainText('Downloading 40%');

  // All bytes arrived but adding failed: Delete download asks first, then removes it.
  state.setDownload({ ...failed, downloaded: true });
  await announce();
  await expect(sheet).toContainText('Downloaded, but couldn’t be added. Retry to finish adding it.');
  await sheet.getByRole('button', { name: /Delete download of Qwen3.8 Flash Next oQ4e-mtp/ }).click();
  const confirm = page.locator('.modal').filter({ hasText: 'from this computer?' });
  await expect(confirm).toContainText('Qwen3.8 Flash Next');
  await confirm.getByRole('button', { name: 'Delete download', exact: true }).click();
  await expect(confirm).toHaveCount(0);
  await expect(qwenDownload).toBeEnabled();

  // Download once more and let it finish: the build is on this computer and offers no Download.
  await qwenDownload.click();
  await expect(sheet).toContainText('Downloading 40%');
  state.finish(qwen.repository);
  await announce();
  await expect(sheet).not.toContainText('Downloading 40%');
  await expect(sheet).toContainText('on this computer');
  await expect(qwenDownload).toHaveCount(0);
  await expect(gemmaDownload).toBeEnabled();

  const deleteRoute = `DELETE /api/providers/local/downloads/${encodeURIComponent(qwen.repository)}`;
  expect(state.calls).toEqual([deleteRoute, 'POST /api/providers/local/downloads', 'POST /api/providers/local/downloads', deleteRoute, 'POST /api/providers/local/downloads']);
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

// The changed build rows under long names, a disk that can't fit a build, a list long enough to scroll, and the
// list's error and empty answers.
test('Add model robustness states', async ({ page }, info) => {
  test.skip(info.project.name !== 'chromium-desktop');
  test.setTimeout(240_000);
  const long = { ...builds[0], name: 'Qwen3.8 Coder Next Instruct Extended Context', repository: 'org/Qwen3.8-Coder-Next-Instruct-Extended-Context-oQ4e-mtp' };
  const blocked = { ...builds[1], disk: { state: 'insufficient', required_bytes: 160e9, free_bytes: 80e9 } };
  const many = ['Gemma Small', 'Llama', 'Mistral', 'Phi', 'Granite', 'Olmo'].flatMap((name, i) => [4, 8].map(bits => ({
    ...builds[2], name, repository: `org/${name.replace(' ', '-')}-oQ${bits}e`, quantization_bits: bits, download_size_gb: 10 + i * 6 + bits, download_size_bytes: (10 + i * 6 + bits) * 1e9, tokens_per_second: 120 - i * 10 - bits,
  })));
  const answers: Record<string, () => object> = {
    long: () => ({ models: [long, builds[2]], recommended_repository: long.repository, disk_free_gb: 200 }),
    disk: () => ({ models: [builds[0], blocked], recommended_repository: builds[0].repository, disk_free_gb: 80 }),
    many: () => ({ models: [...builds, ...many], recommended_repository: builds[0].repository, disk_free_gb: 200 }),
    empty: () => ({ models: [], recommended_repository: null, disk_free_gb: 200 }),
  };
  for (const viewport of [{ name: '1440', width: 1440, height: 900 }, { name: '393', width: 393, height: 852 }]) {
    for (const theme of ['light', 'dark'] as const) {
      for (const state of ['long', 'disk', 'many', 'empty', 'error'] as const) {
        await page.unrouteAll({ behavior: 'ignoreErrors' });
        await page.setViewportSize(viewport);
        await page.emulateMedia({ colorScheme: theme });
        await fixture(page, 'available');
        // Registered after the fixture, so it answers first.
        await page.route('**/api/providers/local/recommendations', route => state === 'error'
          ? route.fulfill({ status: 503, body: '' })
          : route.fulfill({ json: { state: 'ready', memory_gb: 256, memory_in_use_gb: 0, memory_room_gb: 240, downloads: [], ...answers[state]() } }));
        await loginByUrl(page);
        await openSettings(page);
        await openSettingsSection(page, 'Providers');
        const card = page.locator('[data-server-card="omlx-local"]');
        await card.locator('.pr-expand').click();
        await card.getByRole('button', { name: 'Add model', exact: true }).click();
        const sheet = page.locator('.modal').filter({ hasText: 'Add model' });
        if (state === 'long') await expect(sheet).toContainText('Qwen3.8 Coder Next Instruct Extended Context');
        if (state === 'disk') {
          await expect(sheet).toContainText('Needs 160 GB of disk · 80 GB free');
          await expect(sheet.getByRole('button', { name: /Download Qwen3.8 Flash Next oQ6e/ })).toBeDisabled();
        }
        if (state === 'empty') await expect(sheet).toContainText('No recommended models fit this computer.');
        if (state === 'error') {
          await expect(sheet).toContainText('Couldn’t check which models fit this computer.');
          await expect(sheet.getByRole('button', { name: 'Retry' })).toBeVisible();
        }
        if (state === 'many') {
          await expect(sheet).toContainText('Olmo');
          if (shots) await page.screenshot({ path: `${shots}/many-top-${viewport.name}-${theme}.png`, fullPage: true, animations: 'disabled', scale: 'css' });
          await sheet.getByText('Olmo', { exact: true }).scrollIntoViewIfNeeded();
        }
        if (shots) await page.screenshot({ path: `${shots}/${state === 'many' ? 'many-scrolled' : state}-${viewport.name}-${theme}.png`, fullPage: true, animations: 'disabled', scale: 'css' });
      }
    }
  }
});
