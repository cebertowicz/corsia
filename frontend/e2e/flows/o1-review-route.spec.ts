import { existsSync } from 'node:fs';
import { spawn } from 'node:child_process';
import { test, chromium, type Browser, type Page } from '@playwright/test';
import { seed } from '../contact-sheet/document-owner-supervisor';

test('seed and hold reviewer surface', async ({ request }) => {
  test.setTimeout(45 * 60 * 1000);
  const port = Number(process.env.O1_CDP_PORT);
  const stopFile = process.env.O1_STOP_FILE!;
  const profile = process.env.O1_BROWSER_PROFILE!;
  if (!port || !stopFile || !profile || existsSync(stopFile) || existsSync(profile)) throw new Error('Set fresh reviewer route paths and port');
  const child = spawn(chromium.executablePath(), ['--headless=new', '--no-first-run', '--no-default-browser-check', '--remote-debugging-address=127.0.0.1', `--remote-debugging-port=${port}`, `--user-data-dir=${profile}`, 'about:blank'], { stdio: ['ignore', 'ignore', 'pipe'] });
  child.stderr?.on('data', chunk => console.error(`Reviewer Chromium: ${chunk}`));
  let browser: Browser | undefined;
  try {
    const deadline = Date.now() + 20_000;
    let lastError: unknown;
    while (!browser && Date.now() < deadline) {
      if (child.exitCode !== null) throw new Error('Reviewer browser exited during startup');
      try { browser = await chromium.connectOverCDP(`http://127.0.0.1:${port}`, { timeout: 1000 }); }
      catch (error) { lastError = error; await new Promise(resolve => setTimeout(resolve, 250)); }
    }
    if (!browser) throw new Error(`Reviewer browser CDP did not become ready: ${String(lastError)}`);
    const context = browser.contexts()[0];
    if (!context) throw new Error('Reviewer browser has no default context');
    const page: Page = context.pages()[0] || await context.newPage();
    await page.setViewportSize({ width: 1440, height: 1000 });
    const goto = page.goto.bind(page);
    page.goto = (url, options) => goto(new URL(url, `http://127.0.0.1:${process.env.CORSIA_E2E_FRONTEND_PORT}`).toString(), options);
    await seed(page, request);
    console.log(`O1_REVIEW_READY: connect agent-browser to CDP port ${port}; stop by creating ${stopFile}`);
    const end = Date.now() + 30 * 60 * 1000;
    while (!existsSync(stopFile) && Date.now() < end && child.exitCode === null) await new Promise(resolve => setTimeout(resolve, 1000));
    if (child.exitCode !== null) throw new Error('Reviewer browser exited before stop');
  } finally {
    if (browser) await browser.close();
    if (child.exitCode === null) child.kill('SIGTERM');
  }
});
