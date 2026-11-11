import fs from 'node:fs';
import path from 'node:path';
import { type Page, type APIRequestContext } from '@playwright/test';
import { test, expect } from '../support/test';
import { setDebugVersionOverlay } from '../support/heldComments';
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
  // CodeMirror allows scrolling past the document end. Align its last rendered text
  // with the viewport bottom instead of capturing the blank scroll-past-end area.
  if (scroll === 'bottom') {
    await page.locator(selector).evaluate(content => {
      if (!content.classList.contains('cm-content')) return;
      const scroller = content.closest('.cm-scroller');
      const last = [...content.querySelectorAll('.cm-line')].filter(line => line.textContent?.trim()).at(-1);
      if (scroller && last) scroller.scrollTop += last.getBoundingClientRect().bottom - scroller.getBoundingClientRect().bottom + 12;
    });
    await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  }
}

// A passing notice (such as a sent confirmation) can arrive after the screen settles, so each shot dismisses it first.
// Pinned notices are part of the screen's state and stay.
async function dismissToasts(page: Page) {
  const close = page.locator('.toast:not(.toast--pinned) .toast-close');
  for (let left = await close.count(); left > 0; left = await close.count()) {
    await close.first().click();
    await expect(close).toHaveCount(left - 1);
  }
}

const overlaidPanel = (page: Page) => page.locator('aside.pane.overlay:not(.collapsed)');

// Waits until no finite animation or transition is running (the pane's slide, the canvas resizing), then a frame for
// the canvas to re-measure. Endless ones such as spinners are skipped, since they never finish.
async function settle(page: Page) {
  await page.evaluate(async () => {
    const running = () => document.getAnimations().filter(animation => animation.playState === 'running' && animation.effect?.getComputedTiming().iterations !== Infinity);
    for (let left = running(); left.length; left = running()) await Promise.all(left.map(animation => animation.finished.catch(() => undefined)));
    await new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve())));
  });
}

// Where the Workspace panel covers the canvas instead of docking beside it, the screen with it open and with it
// closed are different layouts, so both are shot. Switches to the state not yet shot and returns its file suffix and
// caption, or returns undefined, with the page unchanged, where the panel does not overlay.
async function togglePanelOverlay(page: Page): Promise<{ suffix: string; caption: string } | undefined> {
  const toggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
  if (!(await toggle.isVisible())) return undefined;
  const wasOverlaid = (await overlaidPanel(page).count()) > 0;
  await toggle.click();
  await page.evaluate(() => new Promise<void>(resolve => requestAnimationFrame(() => requestAnimationFrame(() => resolve()))));
  const state = wasOverlaid
    ? { suffix: '-panel-closed', caption: ' · Workspace panel closed' }
    : (await overlaidPanel(page).count()) ? { suffix: '-panel', caption: ' · Workspace panel open' } : undefined;
  if (!state) {
    await toggle.click();
    return undefined;
  }
  if (wasOverlaid) await expect(overlaidPanel(page)).toHaveCount(0);
  await settle(page);
  return state;
}

async function capture(page: Page, request: APIRequestContext, name: string, output: string, selectedWidths?: number[]) {
  if (!/^[a-z0-9-]+$/.test(name) || !path.isAbsolute(output)) throw new Error('Invalid scenario or non-absolute output path');
  const scenario = await import(`./${name}.ts`);
  if (typeof scenario.seed !== 'function' || typeof scenario.scrollAnchor !== 'string') throw new Error('Scenario must export seed and scrollAnchor');
  await setDebugVersionOverlay(request, false);
  const context = page.context();
  // Keep the disposable account ID intact while giving its greeting a review identity.
  await context.routeWebSocket('**/ws', socket => {
    const server = socket.connectToServer();
    server.onMessage(message => {
      if (typeof message !== 'string') return socket.send(message);
      const payload = JSON.parse(message);
      if (payload.type === 'auth_ok' && payload.user) payload.user.name = 'Jordan';
      socket.send(JSON.stringify(payload));
    });
  });
  let captured = page;
  let open: ((page: Page, state?: string) => Promise<void>) | undefined;
  const breakpoints = scanBreakpoints(path.resolve('src'));
  const widths = selectedWidths ?? captureWidths(breakpoints);
  const groups: string[] = [];
  // A scenario may export `states` (names of the screen states it seeds); each is captured at every width into its own folder.
  const states: string[] = Array.isArray(scenario.states) && scenario.states.length ? scenario.states : [''];
  if (!states.every(state => state === '' || /^[a-z0-9-]+$/.test(state))) throw new Error('Invalid scenario state name');
  fs.mkdirSync(output, { recursive: true });
  let shots = 0;
  for (const [state, width] of states.flatMap(state => widths.map(width => [state, width] as const))) {
    const figures: string[] = [];
    const folder = state ? path.join(output, state) : output;
    const href = (file: string) => state ? `${state}/${file}` : file;
    fs.mkdirSync(folder, { recursive: true });
    if (shots++ > 0) {
      await captured.close();
      captured = await context.newPage();
    }
    await captured.setViewportSize({ width, height: 900 });
    if (open) await open(captured, state || undefined);
    else {
      open = await scenario.seed(captured, request);
      if (typeof open !== 'function') throw new Error('Scenario seed must return an open function');
    }
    await expect(captured.locator('.version-overlay')).toHaveCount(0);
    await expect(captured.getByText('Mock Test Model', { exact: true })).toHaveCount(0);
    await expect(captured.locator('body')).not.toContainText(/\be2e\b/i);
    if (typeof scenario.showPicker === 'function') {
      const file = `${width}-rest.png`;
      await captured.screenshot({ path: path.join(folder, file), animations: 'disabled' });
      figures.push(`<figure><figcaption>${state ? `${state} · ` : ''}${width}px · at rest</figcaption><img loading="lazy" src="${href(file)}" alt="${width} pixels, at rest"></figure>`);
      await scenario.showPicker(captured);
    }
    const shoot = async (suffix: string, caption: string) => {
      for (const theme of ['light', 'dark'] as const) {
        await captured.emulateMedia({ colorScheme: theme });
        await expect(captured.locator('html')).toHaveAttribute('data-theme', theme);
        for (const scroll of ['top', 'bottom'] as const) {
          await scrollScreen(captured, scenario.scrollAnchor, scroll);
          await dismissToasts(captured);
          const file = `${width}-${theme}-${scroll}${suffix}.png`;
          await captured.screenshot({ path: path.join(folder, file), animations: 'disabled' });
          figures.push(`<figure><figcaption>${state ? `${state} · ` : ''}${width}px · ${theme} · ${scroll}${caption}</figcaption><img loading="lazy" src="${href(file)}" alt="${state ? `${state}, ` : ''}${width} pixels, ${theme}, ${scroll}${caption}"></figure>`);
        }
      }
    };
    await shoot('', (await overlaidPanel(captured).count()) ? ' · Workspace panel open' : '');
    const otherPanelState = await togglePanelOverlay(captured);
    if (otherPanelState) {
      await shoot(otherPanelState.suffix, otherPanelState.caption);
      await captured.getByRole('button', { name: 'Toggle panel', exact: true }).click();
    }
    groups.push(`<section><h2>${state ? `${state} · ` : ''}${width}px</h2><div>${figures.join('\n')}</div></section>`);
  }
  const html = `<!doctype html><html lang="en"><meta charset="utf-8"><title>${name} contact sheet</title><style>body{font:16px system-ui;margin:24px;background:#eee;color:#111}section>div{display:grid;grid-template-columns:repeat(auto-fit,minmax(240px,1fr));gap:16px}figure{margin:0;background:white;padding:8px}img{width:100%;height:auto}figcaption{padding:8px}</style><h1>${name}</h1><p>Viewport widths: ${widths.join(', ')} px. Widths cover both sides of media-query boundaries.</p><p>Container coverage depends on the scenario content and panes it opens. Where the Workspace panel overlays the screen, it is shot both open and closed.</p>${groups.join('\n')}</html>`;
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
