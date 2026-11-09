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

async function capture(page: Page, request: APIRequestContext, name: string, output: string) {
  if (!/^[a-z0-9-]+$/.test(name) || !path.isAbsolute(output)) throw new Error('Invalid scenario or non-absolute output path');
  const scenario = await import(`./${name}.ts`);
  if (typeof scenario.seed !== 'function' || typeof scenario.scrollAnchor !== 'string') throw new Error('Scenario must export seed and scrollAnchor');
  await page.setViewportSize({ width: 1440, height: 900 });
  await scenario.seed(page, request);
  const breakpoints = scanBreakpoints(path.resolve('src'));
  const widths = captureWidths(breakpoints);
  const groups: string[] = [];
  fs.mkdirSync(output, { recursive: true });
  for (const width of widths) {
    const figures: string[] = [];
    await page.setViewportSize({ width, height: 900 });
    for (const theme of ['light', 'dark'] as const) {
      await page.emulateMedia({ colorScheme: theme });
      await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
      for (const scroll of ['top', 'bottom'] as const) {
        await scrollScreen(page, scenario.scrollAnchor, scroll);
        const file = `${width}-${theme}-${scroll}.png`;
        await page.screenshot({ path: path.join(output, file), animations: 'disabled' });
        figures.push(`<figure><figcaption>${width}px · ${theme} · ${scroll}</figcaption><img loading="lazy" src="${file}" alt="${width} pixels, ${theme}, ${scroll}"></figure>`);
      }
    }
    groups.push(`<section><h2>${width}px</h2><div>${figures.join('\n')}</div></section>`);
  }
  const html = `<!doctype html><html lang="en"><meta charset="utf-8"><title>${name} contact sheet</title><style>body{font:16px system-ui;margin:24px;background:#eee;color:#111}section>div{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}figure{margin:0;background:white;padding:8px}img{width:100%;height:auto}figcaption{padding:8px}</style><h1>${name}</h1><p>Viewport widths: ${widths.join(', ')} px. Media thresholds: ${breakpoints.media.join(', ')} px.</p><p>Container thresholds: ${breakpoints.container.join(', ')} px. A viewport width near a container threshold does not establish coverage of that container.</p>${groups.join('\n')}</html>`;
  fs.writeFileSync(path.join(output, 'index.html'), html);
  console.log(`Contact sheet: ${path.join(output, 'index.html')}`);
}

test.skip(!process.env.CONTACT_SHEET_SCENARIO, 'Run through scripts/contact-sheet');
test('capture contact sheet', async ({ page, request }) => {
  test.setTimeout(1_800_000);
  await capture(page, request, process.env.CONTACT_SHEET_SCENARIO ?? '', process.env.CONTACT_SHEET_OUT ?? '');
});
