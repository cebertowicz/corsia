import { createHash } from 'node:crypto';
import { expect, type Page, type APIRequestContext } from '@playwright/test';
import { loginByUrl, openSettings, openSettingsSection } from '../support/corsia';

/**
 * Settings › System with the Urgent notifications group in each device state. The browser's push APIs and the
 * push/devices answers are faked the way e2e/urgent-push-frames.spec.ts fakes them (copied: importing that spec would
 * register its tests).
 */
export const scrollAnchor = 'section[aria-label="Urgent notifications"]';
const ALL = [
  'off', 'waiting', 'on', 'on-others', 'blocked-browser', 'blocked-phone', 'safari-tab', 'turned-off', 'desktop-app', 'unsupported-browser',
  'pending-this', 'failure-this', 'pending-others', 'failure-others', 'many-long-names', 'same-kind-stopped',
  'confirm-turn-off', 'confirm-remove',
];
// URGENT_PUSH_STATES (comma list) limits a run to some states, so a long sheet can be taken in parts.
export const states = process.env.URGENT_PUSH_STATES ? process.env.URGENT_PUSH_STATES.split(',') : ALL;

const ENDPOINT = 'https://fcm.googleapis.com/fcm/send/FRAME-FAKE';
export const ENDPOINT_ID = createHash('sha256').update(ENDPOINT).digest('hex');
const IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1';

type Device = { id: string; name: string; state: 'on' | 'turned_off'; endpoint_id: string; added_at?: string };
type Setup = {
  permission?: 'default' | 'denied';
  answer?: 'granted' | 'denied' | 'pending';
  ios?: 'tab' | 'app';
  noPush?: boolean;
  desktopApp?: boolean;
  subscribed?: boolean;
  /** The subscription's endpoint; its host names the company that delivers it. */
  endpoint?: string;
  devices?: Device[];
  tap?: boolean;
  /** The server never answers a DELETE, or answers it with a failure. */
  hold?: boolean;
  fail?: boolean;
  /** Press Turn off / Remove on both other devices (iPhone, Chrome on Mac). */
  both?: boolean;
  /** Open the inline confirm on the iPhone's Turn off or the Mac's Remove, and leave it open. */
  ask?: 'turn-off' | 'remove';
};
const mine = (state: 'on' | 'turned_off'): Device => ({ id: 'this', name: 'This browser', state, endpoint_id: ENDPOINT_ID });
const dev = (id: string, name: string, state: 'on' | 'turned_off' = 'on', day = 24): Device => ({ id, name, state, endpoint_id: id, added_at: `2026-11-${String(day).padStart(2, '0')}T15:00:00Z` });

export const setups: Record<string, Setup> = {
  'off': {},
  'waiting': { answer: 'pending', tap: true },
  'on': { answer: 'granted', tap: true },
  'on-others': { subscribed: true, devices: [mine('on'), dev('p1', 'iPhone'), dev('m1', 'Chrome on Mac', 'turned_off')] },
  'blocked-browser': { answer: 'denied', tap: true },
  'blocked-phone': { permission: 'denied', ios: 'app' },
  'safari-tab': { ios: 'tab', noPush: true },
  'turned-off': { subscribed: true, devices: [mine('turned_off')] },
  'desktop-app': { desktopApp: true, noPush: true },
  'unsupported-browser': { noPush: true },
  'same-kind-stopped': { subscribed: true, devices: [mine('on'), dev('s1', 'iPhone', 'turned_off', 19), dev('s2', 'iPhone', 'turned_off', 26), dev('s3', 'iPhone', 'on', 24)] },
  'pending-this': { subscribed: true, devices: [mine('on')], hold: true, tap: true },
  'failure-this': { subscribed: true, devices: [mine('on')], fail: true, tap: true },
  'pending-others': { subscribed: true, devices: [mine('on'), dev('p1', 'iPhone'), dev('m1', 'Chrome on Mac', 'turned_off')], hold: true, both: true },
  'failure-others': { subscribed: true, devices: [mine('on'), dev('p1', 'iPhone'), dev('m1', 'Chrome on Mac', 'turned_off')], fail: true, both: true },
  'confirm-turn-off': { subscribed: true, devices: [mine('on'), dev('p1', 'iPhone'), dev('m1', 'Chrome on Mac', 'turned_off')], ask: 'turn-off' },
  'confirm-remove': { subscribed: true, devices: [mine('on'), dev('p1', 'iPhone'), dev('m1', 'Chrome on Mac', 'turned_off')], ask: 'remove' },
  'many-long-names': {
    subscribed: true,
    devices: [
      mine('on'),
      dev('d1', 'Jordan’s extremely long named iPhone 16 Pro Max with the cracked screen protector'),
      dev('d2', 'Firefox on the shared Windows workstation in the east wing of the second floor office', 'turned_off'),
      dev('d3', 'Chrome on Mac', 'on', 18), dev('d4', 'Chrome on Mac', 'turned_off', 26), dev('d5', 'iPhone', 'on', 24), dev('d6', 'iPhone', 'on', 26),
      dev('d7', 'Chrome on Android tablet in the kitchen'), dev('d8', 'Edge on Surface Laptop 7', 'turned_off'),
    ],
  },
};

export async function install(page: Page, setup: Setup) {
  await page.addInitScript(({ s, endpoint, iphone }) => {
    const w = window as unknown as Record<string, unknown>;
    let permission: string = s.permission ?? 'default';
    let sub: unknown = s.subscribed ? makeSub() : null;
    function makeSub() {
      return { endpoint, toJSON: () => ({ endpoint, keys: { p256dh: 'FAKE', auth: 'FAKE' } }), unsubscribe: async () => { sub = null; return true; } };
    }
    const registration = { pushManager: { getSubscription: async () => sub, subscribe: async () => { sub = makeSub(); return sub; } } };
    Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: {
      getRegistrations: async () => [], getRegistration: async () => registration, register: async () => registration, ready: Promise.resolve(registration),
      addEventListener: () => undefined, removeEventListener: () => undefined,
    } });
    if (s.noPush) delete (window as unknown as { PushManager?: unknown }).PushManager;
    else w.PushManager = w.PushManager ?? function PushManager() {};
    w.Notification = class { static get permission() { return permission; }
      static async requestPermission() {
        if (s.answer === 'pending') return new Promise(() => {});
        permission = s.answer === 'denied' ? 'denied' : 'granted';
        return permission;
      } };
    if (s.ios) {
      Object.defineProperty(navigator, 'userAgent', { configurable: true, get: () => iphone });
      Object.defineProperty(navigator, 'standalone', { configurable: true, value: s.ios === 'app' });
    }
    if (s.desktopApp) w.__CORSIA_SHELL__ = { mode: 'local' };
  }, { s: setup, endpoint: setup.endpoint ?? ENDPOINT, iphone: IPHONE });
  let devices = setup.devices ?? [];
  await page.route('**/api/push/devices**', async (route) => {
    const method = route.request().method();
    if (method === 'PUT') devices = [mine('on'), ...devices.filter((d) => d.id !== 'this')];
    if (method === 'DELETE') {
      if (setup.hold) return;
      if (setup.fail) return route.fulfill({ status: 500, json: { detail: 'Internal Server Error' } });
      const gone = decodeURIComponent(route.request().url().split('/').pop() ?? '');
      devices = devices.filter((d) => d.id !== gone);
    }
    await route.fulfill({ json: { ok: true } });
  });
  await page.route('**/api/push', (route) => route.fulfill({ json: { public_key: 'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U', devices } }));
}

let current = '';
export async function openState(page: Page, state: string) {
  current = state;
  const setup = setups[state];
  if (!setup) throw new Error(`Unknown state ${state}`);
  await install(page, setup);
  await loginByUrl(page, { waitForComposer: false });
  await openSettings(page);
  await openSettingsSection(page, 'System');
  const group = page.getByRole('region', { name: 'Urgent notifications' });
  await expect(group).toBeVisible();
  await expect(group.getByText('Loading…')).toHaveCount(0);
  if (setup.tap) await group.getByRole('button', { name: 'Urgent notifications on this device' }).click();
  if (setup.both) {
    // Each action asks first; the second press, on the same name, is the confirm.
    await group.getByRole('button', { name: /^Turn off urgent notifications on iPhone/ }).click();
    await group.getByRole('button', { name: /^Turn off urgent notifications on iPhone/ }).click();
    await group.getByRole('button', { name: /^Remove Chrome on Mac/ }).click();
    await group.getByRole('button', { name: /^Remove Chrome on Mac/ }).click();
  }
  if (setup.ask === 'turn-off') await group.getByRole('button', { name: /^Turn off urgent notifications on iPhone/ }).click();
  if (setup.ask === 'remove') await group.getByRole('button', { name: /^Remove Chrome on Mac/ }).click();
  await page.mouse.move(0, 0);
  await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
  await page.waitForTimeout(500);
}

export async function seed(page: Page, _request: APIRequestContext) {
  await openState(page, states[0]);
  return (target: Page, state?: string) => openState(target, state ?? states[0]);
}

/**
 * Keyboard order, focus ring, target sizes, accessible names and state-line contrast, measured on the running screen and
 * written to URGENT_PUSH_MEASURE_OUT as JSON; the shot that follows shows the toggle with keyboard focus.
 */
export const variants = {
  focus: {
    widths: [393, 1440],
    apply: async (page: Page) => {
      const out = process.env.URGENT_PUSH_MEASURE_OUT;
      const group = page.getByRole('region', { name: 'Urgent notifications' });
      const result: Record<string, unknown> = { state: current, width: page.viewportSize()?.width };
      result.aria = await group.ariaSnapshot().catch((e) => `ariaSnapshot failed: ${e}`);
      // Tab order: start on the control above the group, press Tab until focus leaves the group.
      await page.getByTestId('devices-row').scrollIntoViewIfNeeded();
      await page.evaluate(() => {
        const row = document.querySelector('[data-testid="devices-row"]');
        const first = row?.querySelector('button, [tabindex]') as HTMLElement | null;
        first?.focus();
      });
      const order: unknown[] = [];
      let left = false;
      for (let i = 0; i < 14 && !left; i++) {
        await page.keyboard.press('Tab');
        const info = await page.evaluate(() => {
          const el = document.activeElement as HTMLElement | null;
          if (!el) return null;
          const inside = !!el.closest('section[aria-label="Urgent notifications"]');
          const r = el.getBoundingClientRect();
          const cs = getComputedStyle(el);
          return { tag: el.tagName, role: el.getAttribute('role'), name: el.getAttribute('aria-label') ?? el.textContent?.trim().slice(0, 50), inside,
            w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10,
            focusVisible: el.matches(':focus-visible'), outline: `${cs.outlineStyle} ${cs.outlineWidth} ${cs.outlineColor}`, boxShadow: cs.boxShadow };
        });
        order.push(info);
        if (info && !info.inside && order.length > 1 && (order[order.length - 2] as { inside?: boolean } | null)?.inside) left = true;
      }
      result.tabOrder = order;
      // Contrast of every line of text in the group, in both themes.
      const contrast = async (theme: 'light' | 'dark') => {
        await page.emulateMedia({ colorScheme: theme });
        await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
        return page.evaluate(() => {
          const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
          const ctx = canvas.getContext('2d', { willReadFrequently: true })!;
          const rgba = (c: string) => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = '#000'; ctx.fillStyle = c; ctx.fillRect(0, 0, 1, 1); const d = ctx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
          const lum = ([r, g, b]: number[]) => { const f = (v: number) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
          const bgOf = (el: HTMLElement) => {
            const layers: number[][] = [];
            for (let e: HTMLElement | null = el; e; e = e.parentElement) { const c = rgba(getComputedStyle(e).backgroundColor); if (c[3] > 0) layers.push(c); if (c[3] >= 1) break; }
            let base = [255, 255, 255];
            if (!layers.length || layers[layers.length - 1][3] < 1) base = rgba(getComputedStyle(document.body).backgroundColor).slice(0, 3);
            for (const c of layers.reverse()) base = base.map((v, i) => c[i] * c[3] + v * (1 - c[3]));
            return base;
          };
          const section = document.querySelector('section[aria-label="Urgent notifications"]')!;
          const rows: unknown[] = [];
          const walker = document.createTreeWalker(section, NodeFilter.SHOW_TEXT);
          for (let n = walker.nextNode(); n; n = walker.nextNode()) {
            const text = n.textContent?.trim(); if (!text) continue;
            const el = n.parentElement as HTMLElement; const cs = getComputedStyle(el);
            const fg = rgba(cs.color); const bg = bgOf(el);
            const fgc = fg.slice(0, 3).map((v, i) => v * fg[3] + bg[i] * (1 - fg[3]));
            const [a, b] = [lum(fgc), lum(bg)];
            rows.push({ text: text.slice(0, 48), size: cs.fontSize, ratio: Math.round(((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)) * 100) / 100, fg: fgc.map(Math.round), bg: bg.map(Math.round), cls: el.className });
          }
          // The switch track against the page: is "on/off" carried by more than colour?
          const toggles = [...section.querySelectorAll('button[role="switch"], [role="switch"]')].map((t) => { const r = t.getBoundingClientRect(); const cs = getComputedStyle(t as HTMLElement); return { label: t.getAttribute('aria-label'), checked: t.getAttribute('aria-checked'), disabled: (t as HTMLButtonElement).disabled, w: r.width, h: r.height, bg: cs.backgroundColor, border: cs.border }; });
          return { rows, toggles };
        });
      };
      result.contrastLight = await contrast('light');
      result.contrastDark = await contrast('dark');
      // Every control in the group: size on this width.
      result.controls = await page.evaluate(() => [...document.querySelectorAll('section[aria-label="Urgent notifications"] button, section[aria-label="Urgent notifications"] [role="switch"]')].map((e) => { const r = e.getBoundingClientRect(); return { name: e.getAttribute('aria-label') ?? e.textContent?.trim(), w: Math.round(r.width * 10) / 10, h: Math.round(r.height * 10) / 10, role: e.getAttribute('role'), pointer: getComputedStyle(e).cursor }; }));
      // Row geometry: do long names wrap or shorten?
      result.rows = await page.evaluate(() => [...document.querySelectorAll('section[aria-label="Urgent notifications"] > *, section[aria-label="Urgent notifications"] [class*="row"]')].slice(0, 30).map((e) => { const r = e.getBoundingClientRect(); return { cls: (e as HTMLElement).className?.toString().slice(0, 40), text: e.textContent?.trim().slice(0, 60), h: Math.round(r.height), w: Math.round(r.width) }; }));
      if (out) { const fs = await import('node:fs'); fs.mkdirSync(out, { recursive: true }); fs.writeFileSync(`${out}/${current}-${result.width}.json`, JSON.stringify(result, null, 1)); }
      // The switch with keyboard focus, as a crop of the group in each theme.
      if (out) {
        const fsx = await import('node:fs');
        await page.evaluate(() => { const row = document.querySelector('[data-testid="devices-row"]'); (row?.querySelector('button, [tabindex]') as HTMLElement | null)?.focus(); });
        for (let i = 0; i < 4; i++) {
          await page.keyboard.press('Tab');
          if (await page.evaluate(() => document.activeElement?.getAttribute('aria-label') === 'Urgent notifications on this device')) break;
        }
        if (await page.evaluate(() => document.activeElement?.getAttribute('aria-label') === 'Urgent notifications on this device')) {
          await group.scrollIntoViewIfNeeded();
          for (const theme of ['light', 'dark'] as const) {
            await page.emulateMedia({ colorScheme: theme });
            await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
            const box = await group.boundingBox();
            const vp = page.viewportSize()!;
            if (box) await page.screenshot({ path: `${out}/${current}-${result.width}-focus-${theme}.png`, clip: { x: Math.max(0, box.x - 8), y: Math.max(0, box.y - 8), width: Math.min(vp.width - Math.max(0, box.x - 8), box.width + 16), height: Math.min(vp.height - Math.max(0, box.y - 8), Math.min(box.height + 16, 260)) }, animations: 'disabled' });
          }
          fsx.writeFileSync(`${out}/${current}-${result.width}-focus-ok`, '1');
        }
      }
      // Leave keyboard focus on the first switch so the shot shows the ring.
      await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
      await page.getByTestId('devices-row').scrollIntoViewIfNeeded();
      for (let i = 0; i < 6; i++) {
        await page.keyboard.press('Tab');
        if (await page.evaluate(() => !!document.activeElement?.closest('section[aria-label="Urgent notifications"]'))) break;
      }
    },
  },
};
