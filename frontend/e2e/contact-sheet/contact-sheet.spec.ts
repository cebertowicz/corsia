import fs from 'node:fs';
import path from 'node:path';
import { type Page, type APIRequestContext } from '@playwright/test';
import { test, expect } from '../support/test';
import { scanBreakpoints, captureWidths } from './breakpoints.mjs';
async function scrollScreen(page: Page, selector: string, scroll: 'top' | 'bottom') {
  await page.evaluate(({ selector, scroll }) => {
    const anchor = document.querySelector(selector);
    if (!anchor) throw new Error('Scenario content is missing');
    let current: HTMLElement | null = anchor as HTMLElement;
    while (current) {
      const overflow = getComputedStyle(current).overflowY;
      if ((overflow === 'auto' || overflow === 'scroll') && current.scrollHeight > current.clientHeight) break;
      current = current.parentElement;
    }
    if (!current) throw new Error('Scenario has no scrollable content');
    current.scrollTop = scroll === 'top' ? 0 : current.scrollHeight;
  }, { selector, scroll });
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
}

async function capture(page: Page, request: APIRequestContext, name: string, output: string, selectedWidths?: number[]) {
  if (!/^[a-z0-9-]+$/.test(name) || !path.isAbsolute(output)) throw new Error('Invalid scenario or non-absolute output path');
  const scenario = await import(`./${name}.ts`);
  if (typeof scenario.seed !== 'function' || typeof scenario.scrollAnchor !== 'string') throw new Error('Scenario must export seed and scrollAnchor');
  const context = page.context();
  let captured = page;
  let open: ((page: Page) => Promise<void>) | undefined;
  const breakpoints = scanBreakpoints(path.resolve('src'));
  const widths = selectedWidths ?? captureWidths(breakpoints);
  const groups: string[] = [];
  fs.mkdirSync(output, { recursive: true });
  for (const [index, width] of widths.entries()) {
    const figures: string[] = [];
    if (index > 0) {
      await captured.close();
      captured = await context.newPage();
    }
    await captured.setViewportSize({ width, height: 900 });
    if (open) await open(captured);
    else {
      open = await scenario.seed(captured, request);
      if (typeof open !== 'function') throw new Error('Scenario seed must return an open function');
    }
    for (const theme of ['light', 'dark'] as const) {
      await captured.emulateMedia({ colorScheme: theme });
      await expect(captured.locator('html')).toHaveAttribute('data-theme', theme);
      for (const scroll of ['top', 'bottom'] as const) {
        await scrollScreen(captured, scenario.scrollAnchor, scroll);
        const file = `${width}-${theme}-${scroll}.png`;
        await captured.screenshot({ path: path.join(output, file), animations: 'disabled' });
        figures.push(`<figure><figcaption>${width}px · ${theme} · ${scroll}</figcaption><img loading="lazy" src="${file}" alt="${width} pixels, ${theme}, ${scroll}"></figure>`);
      }
    }
    groups.push(`<section><h2>${width}px</h2><div>${figures.join('\n')}</div></section>`);
  }
  const html = `<!doctype html><html lang="en"><meta charset="utf-8"><title>${name} contact sheet</title><style>body{font:16px system-ui;margin:24px;background:#eee;color:#111}section>div{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}figure{margin:0;background:white;padding:8px}img{width:100%;height:auto}figcaption{padding:8px}</style><h1>${name}</h1><p>Viewport widths: ${widths.join(', ')} px. Widths cover both sides of media-query boundaries.</p><p>Container coverage depends on the scenario content and panes it opens.</p>${groups.join('\n')}</html>`;
  fs.writeFileSync(path.join(output, 'index.html'), html);
  console.log(`Contact sheet: ${path.join(output, 'index.html')}`);
  return captured;
}

test.use({ seedPerTest: true });
test.skip(!process.env.CONTACT_SHEET_SCENARIO, 'Run through scripts/contact-sheet');
test('capture contact sheet', async ({ page, request }) => {
  test.skip(process.env.CONTACT_SHEET_REGRESSION === '1', 'Regression test only');
  test.setTimeout(1_800_000);
  await capture(page, request, process.env.CONTACT_SHEET_SCENARIO ?? '', process.env.CONTACT_SHEET_OUT ?? '');
});

test('chat composer initializes at the captured width', async ({ page, request }) => {
  test.skip(process.env.CONTACT_SHEET_SCENARIO !== 'chat', 'Chat regression only');
  test.setTimeout(120_000);
  const output = path.join(process.env.CONTACT_SHEET_OUT ?? '', 'regression');
  const captured = await capture(page, request, 'chat', output, [1440, 1440, 393]);
  await expect(captured.locator('aside.sidebar').getByText(/^Review conversation \d+$/, { exact: true })).toHaveCount(1);
  await captured.getByRole('button', { name: 'Attach', exact: true }).click();
  await expect(captured.locator('.mobile-attach-row')).toBeVisible();
  await captured.screenshot({ path: path.join(output, 'composer-393.png') });
});
