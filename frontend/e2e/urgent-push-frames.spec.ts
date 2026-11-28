import { createHash } from 'node:crypto';
import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, type Locator, type Page } from '@playwright/test';
import { test } from './support/test';
import { createSession, loginByUrl, openSettings, openSettingsSection } from './support/corsia';
import { setDebugVersionOverlay } from './support/heldComments';

/**
 * Design frames for the urgent notifications control, round 2: the real Settings › System page on a disposable
 * instance, with the test build strip off. Only the browser's push APIs (permission, worker registration,
 * subscribe: no network leaves the machine) and the push and devices answers are faked; each state is reached by
 * using the screen. Light at 393 and 1440; the states the round changed also at 393 in dark. Output: PUSH_FRAMES_OUT.
 */
const OUT = process.env.PUSH_FRAMES_OUT ?? join(process.cwd(), 'e2e-results', 'urgent-push-frames-r2');
const SIZES = { w1440: { width: 1440, height: 1000 }, w393: { width: 393, height: 852 }, w380: { width: 380, height: 852 } };
const GOOGLE = 'https://fcm.googleapis.com/fcm/send/FRAME-FAKE';
const APPLE = 'https://web.push.apple.com/FRAME-FAKE';
const id = (endpoint: string) => createHash('sha256').update(endpoint).digest('hex');
const IPHONE = 'Mozilla/5.0 (iPhone; CPU iPhone OS 18_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/18.0 Mobile/15E148 Safari/604.1';

type Device = { id: string; name: string; state: 'on' | 'turned_off'; endpoint_id: string; added_at: string };
type Setup = {
	/** The OS permission before the tap, and what the prompt answers: granted, denied, never (held open), or manual (the test answers it). */
	permission?: 'default' | 'denied';
	answer?: 'granted' | 'denied' | 'pending' | 'manual';
	ios?: 'tab' | 'app';
	noPush?: boolean;
	desktopApp?: boolean;
	/** The browser already holds a subscription. */
	subscribed?: boolean;
	devices?: Device[];
	readOnly?: boolean;
	/** The server never answers a DELETE, or answers it with a failure. */
	hold?: boolean;
	fail?: boolean;
	/** A tall viewport, so a long list shows whole. */
	tall?: boolean;
};
type Frame = { id: string; setup: Setup; act?: (page: Page) => Promise<void>; text: string; line: string; dark?: boolean; keepFocus?: boolean };

const endpointOf = (s: Setup) => (s.ios ? APPLE : GOOGLE);
const switchOf = (page: Page) => page.getByRole('button', { name: 'Urgent notifications on this device' });
const tap = async (page: Page) => { await switchOf(page).click(); };
const day = (d: number) => `2026-11-${String(d).padStart(2, '0')}T15:00:00Z`;
const mine = (state: 'on' | 'turned_off', endpoint = GOOGLE): Device => ({ id: 'this', name: 'This browser', state, endpoint_id: id(endpoint), added_at: day(20) });
const dev = (key: string, name: string, added: number, state: 'on' | 'turned_off' = 'on'): Device => ({ id: key, name, state, endpoint_id: key, added_at: day(added) });
const phone = dev('p1', 'iPhone', 24);
const mac = dev('m1', 'Chrome on Mac', 22, 'turned_off');
const others = [mine('on'), phone, mac];
const turnOffPhone = (page: Page) => page.getByRole('button', { name: 'Turn off urgent notifications on iPhone, added Nov 24' });
const removeMac = (page: Page) => page.getByRole('button', { name: 'Remove Chrome on Mac, added Nov 22' });
const bothOthers = async (page: Page) => { await turnOffPhone(page).click(); await removeMac(page).click(); };

const frames: Frame[] = [
	{ id: 'off', setup: {}, text: 'This device', line: 'Off: the switch is off and no line under it; nothing prompted on load; the hint names no company.', dark: true },
	{ id: 'waiting', setup: { answer: 'pending' }, act: tap, text: 'Waiting for your permission…', line: 'The OS prompt is open (here held open): the switch reads Off and locked, and Cancel is the way out.', dark: true },
	{ id: 'on', setup: { answer: 'granted' }, act: tap, text: 'This device', line: 'On: reached by tapping, permission granted, subscription saved; no line under it, the switch shows it.', dark: true },
	{ id: 'on-others', setup: { subscribed: true, devices: others }, text: 'Stopped on its own. Turn on there.', line: 'On here, with two other devices listed: one on with its date, one that stopped on its own.', dark: true },
	{ id: 'blocked-browser', setup: { answer: 'denied' }, act: tap, text: 'Blocked in your browser’s settings', line: 'Permission denied at the prompt: Off, the setting named, no re-prompt.' },
	{ id: 'blocked-phone', setup: { permission: 'denied', ios: 'app' }, text: 'Allow CORSIA in Settings › Notifications.', line: 'Permission already denied on an iPhone Home Screen app: names Settings › Notifications.' },
	{ id: 'safari-tab', setup: { ios: 'tab', noPush: true }, text: 'Works in the Home Screen app only', line: 'iPhone in a Safari tab: one line, no switch.' },
	{ id: 'turned-off', setup: { subscribed: true, devices: [mine('turned_off')] }, text: 'Stopped on its own. Turn on again.', line: 'After the delivery company stops accepting this device: Off, the line says it stopped on its own and what to do, the switch ready to turn on again.', dark: true },
	{ id: 'desktop-app', setup: { desktopApp: true, noPush: true }, text: 'Not available in this app', line: 'Desktop app with no PushManager: one line, no switch.' },
	{ id: 'read-only-switch-works', setup: { readOnly: true, ios: 'app' }, text: 'Manage connections on', line: 'The connections row is read-only ("Manage connections on … itself.") and the notification switch beside it still turns on.', act: tap },
	{
		id: 'many-long-names', dark: true, text: 'On · Added Nov 26', line: 'Nine devices: long names on one line, shortened in the middle, and the two iPhones told apart by the date on the second line.',
		setup: {
			tall: true,
			subscribed: true,
			devices: [
				mine('on'),
				dev('d1', 'Jordan’s extremely long named iPhone 16 Pro Max with the cracked screen protector', 21),
				dev('d2', 'Firefox on the shared Windows workstation in the east wing of the second floor office', 19, 'turned_off'),
				dev('d3', 'Chrome on Mac', 18), dev('d4', 'Chrome on Mac', 26, 'turned_off'),
				dev('d5', 'iPhone', 24), dev('d6', 'iPhone', 26),
				dev('d7', 'Chrome on Android tablet in the kitchen', 23), dev('d8', 'Edge on Surface Laptop 7', 25, 'turned_off'),
			],
		},
	},
	{ id: 'pending-others', setup: { subscribed: true, devices: others, hold: true }, act: bothOthers, text: 'Removing…', line: 'Turn off on iPhone and Remove on Chrome on Mac sent, the server not yet answered: both rows say what is happening and their actions are dimmed and inert.', dark: true },
	{ id: 'failure-others', setup: { subscribed: true, devices: others, fail: true }, act: bothOthers, text: 'Couldn’t remove Chrome on Mac. Try again.', line: 'The same two actions, the server refusing: each row says what failed, in its own words, with the next step; the actions are ready to press again.', dark: true },
	{ id: 'pending-this', setup: { subscribed: true, devices: [mine('on')], hold: true }, act: tap, text: 'Turning off…', line: 'The switch turned off on this device, the server not yet answered: the switch stays On and locked until it is true.', dark: true },
	{ id: 'failure-this', setup: { subscribed: true, devices: [mine('on')], fail: true }, act: tap, text: 'Couldn’t turn off notifications on this device. Try again.', line: 'The same, the server refusing: the switch is still On, the row says what failed and what to do.', dark: true },
	{
		id: 'focus-ring', setup: { subscribed: true, devices: others }, keepFocus: true, dark: true, text: 'Turn off',
		line: 'Keyboard focus on Turn off for iPhone: the app’s 2 px accent ring, as on the switch.',
		act: async (page) => {
			// Another control on the page can take focus while Settings finishes loading: scroll to the switch and focus it until it holds.
			await switchOf(page).scrollIntoViewIfNeeded();
			await expect(async () => {
				await switchOf(page).focus();
				expect(await page.evaluate(() => document.activeElement?.getAttribute('aria-label'))).toBe('Urgent notifications on this device');
			}).toPass({ timeout: 8000 });
			await page.keyboard.press('Tab');
			expect(await page.evaluate(() => document.activeElement?.getAttribute('aria-label')), 'the element focused after Tab from the switch').toContain('Turn off urgent notifications on iPhone');
		},
	},
];

test.describe.configure({ mode: 'serial' });
test.beforeAll(() => mkdirSync(join(OUT, 'measure'), { recursive: true }));
test.beforeEach(async ({ request }) => { await setDebugVersionOverlay(request, false); });

async function install(page: Page, setup: Setup) {
	const endpoint = endpointOf(setup);
	await page.addInitScript(({ s, endpoint, iphone }) => {
		const w = window as unknown as Record<string, unknown>;
		let permission: string = s.permission ?? 'default';
		let sub: unknown = s.subscribed ? makeSub() : null;
		function makeSub() {
			return { endpoint, toJSON: () => ({ endpoint, keys: { p256dh: 'FAKE', auth: 'FAKE' } }), unsubscribe: async () => { sub = null; return true; } };
		}
		const registration = { pushManager: { getSubscription: async () => sub, subscribe: async () => { sub = makeSub(); return sub; } } };
		Object.defineProperty(navigator, 'serviceWorker', { configurable: true, value: {
			getRegistrations: async () => [],
			getRegistration: async () => registration,
			register: async () => registration,
			ready: Promise.resolve(registration),
		} });
		if (s.noPush) delete (window as unknown as { PushManager?: unknown }).PushManager;
		else w.PushManager = w.PushManager ?? function PushManager() {};
		w.Notification = class { static get permission() { return permission; }
			static async requestPermission() {
				if (s.answer === 'pending') return new Promise(() => {});
				if (s.answer === 'manual') return new Promise((resolve) => { w.__answerPrompt = (answer: string) => { permission = answer; resolve(answer); }; });
				permission = s.answer === 'denied' ? 'denied' : 'granted';
				return permission;
			} };
		if (s.ios) {
			Object.defineProperty(navigator, 'userAgent', { configurable: true, get: () => iphone });
			Object.defineProperty(navigator, 'standalone', { configurable: true, value: s.ios === 'app' });
		}
		if (s.desktopApp) w.__CORSIA_SHELL__ = { mode: 'local' };
	}, { s: setup, endpoint, iphone: IPHONE });
	let devices = setup.devices ?? [];
	const puts: string[] = [];
	await page.route('**/api/push/devices**', async (route) => {
		const method = route.request().method();
		if (method === 'PUT') {
			puts.push('PUT');
			devices = [mine('on', endpoint), ...devices.filter((d) => d.id !== 'this')];
			return route.fulfill({ json: { ok: true } });
		}
		if (method === 'DELETE') {
			if (setup.hold) return;
			if (setup.fail) return route.fulfill({ status: 500, json: { detail: 'Internal Server Error' } });
			const gone = decodeURIComponent(route.request().url().split('/').pop() ?? '');
			devices = devices.filter((d) => d.id !== gone);
		}
		return route.fulfill({ json: { ok: true } });
	});
	await page.route('**/api/push', (route) => route.fulfill({ json: { public_key: 'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U', devices } }));
	if (setup.readOnly) {
		await page.route('**/api/devices', (route) => route.request().method() === 'GET'
			? route.fulfill({ json: { enabled: true, listening: { port: 47901 }, announced_name: 'Studio', local_hostname: 'studio.local', computer_name: 'Studio', lan_addresses: ['192.168.1.20'], code: { state: 'none' }, read_only: true } })
			: route.continue());
	}
	return { setDevices: (next: Device[]) => { devices = next; }, puts };
}

async function theme(page: Page, scheme: 'light' | 'dark') {
	await page.emulateMedia({ colorScheme: scheme });
	await expect(page.locator('html')).toHaveAttribute('data-theme', scheme);
}

async function openGroup(page: Page, setup: Setup, size: { width: number; height: number }, scheme: 'light' | 'dark') {
	const controls = await install(page, setup);
	await page.emulateMedia({ colorScheme: scheme });
	await loginByUrl(page, { waitForComposer: false });
	await theme(page, scheme);
	await page.setViewportSize(setup.tall ? { width: size.width, height: size.height + 900 } : size);
	await openSettings(page);
	await openSettingsSection(page, 'System');
	const group = page.getByRole('region', { name: 'Urgent notifications' });
	await expect(group).toBeVisible();
	await expect(group.getByText('Loading…')).toHaveCount(0);
	return { group, controls };
}

/** The pointer and focus stay off the rows so no frame shows a hover or focus tint that isn't the design's. */
async function settle(page: Page, keepFocus = false) {
	await page.mouse.move(0, 0);
	if (!keepFocus) await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
	await page.waitForTimeout(600);
}

/** Row heights, line counts, shortened names and the Cancel-to-switch gap, read from the real group. */
async function measureGroup(group: Locator) {
	return group.evaluate((section) => {
		/** Lines of visible text: client rects of its text nodes (not the screen-reader-only ones) merged where they overlap vertically. */
		const lines = (el: Element | null) => {
			if (!el) return 0;
			const rects: DOMRect[] = [];
			const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
			for (let node = walker.nextNode(); node; node = walker.nextNode()) {
				if (!node.textContent?.trim() || node.parentElement?.closest('.sr-only')) continue;
				const range = document.createRange();
				range.selectNodeContents(node);
				rects.push(...range.getClientRects());
			}
			rects.sort((x, y) => x.top - y.top);
			let count = 0;
			let bottom = -Infinity;
			for (const r of rects) { if (r.top >= bottom - 1) { count++; bottom = r.bottom; } else bottom = Math.max(bottom, r.bottom); }
			return count;
		};
		const box = (el: Element | null) => { if (!el) return null; const r = el.getBoundingClientRect(); return { left: r.left, right: r.right, top: r.top, bottom: r.bottom, width: r.width, height: r.height }; };
		const hint = section.querySelector('.hint');
		const rows = [...section.querySelectorAll('.settings-row')].map((row) => {
			const head = row.querySelector('.head') as HTMLElement | null;
			const sub = row.querySelector('.sub');
			return {
				name: row.querySelector('.name')?.textContent?.trim(),
				height: row.getBoundingClientRect().height,
				nameLines: lines(row.querySelector('.name')),
				nameShortened: head ? head.scrollWidth > head.clientWidth : false,
				title: row.querySelector('.mid')?.getAttribute('title') ?? null,
				sub: sub ? [...sub.childNodes].map((n) => (n instanceof Element && n.querySelector('.sr-only') ? n.firstChild?.textContent : n.textContent)).join(' ').replace(/\s+/g, ' ').trim() : null,
				subLines: lines(sub),
				subClipped: sub ? (sub as HTMLElement).scrollWidth > (sub as HTMLElement).clientWidth + 1 : false,
			};
		});
		const cancel = section.querySelector('button.cancel');
		const sw = section.querySelector('button[aria-label="Urgent notifications on this device"]');
		const gap = cancel && sw ? Math.round((sw.getBoundingClientRect().left - cancel.getBoundingClientRect().right) * 10) / 10 : null;
		return { viewport: window.innerWidth, hint: hint?.textContent, hintLines: lines(hint), rows, cancel: box(cancel), switchTrack: box(sw), cancelToSwitchGap: gap };
	});
}

const shots: Array<{ frame: Frame; size: string; dims: { width: number; height: number }; scheme: 'light' | 'dark' }> = [];
for (const frame of frames) {
	for (const [size, dims] of Object.entries(SIZES)) {
		shots.push({ frame, size, dims, scheme: 'light' });
		if (frame.dark && size === 'w393') shots.push({ frame, size, dims, scheme: 'dark' });
	}
}

for (const { frame, size, dims, scheme } of shots) {
	test(`frame ${frame.id} ${size} ${scheme}`, async ({ page }) => {
		const { group } = await openGroup(page, frame.setup, dims, scheme);
		if (frame.act) await frame.act(page);
		await group.evaluate((el) => el.scrollIntoView({ block: 'center' }));
		await settle(page, frame.keepFocus);
		const short = frame.text.length <= 3;
		const scope = frame.id === 'read-only-switch-works' ? page : group;
		await expect((short ? scope.getByText(frame.text, { exact: true }) : scope.getByText(frame.text, { exact: false })).first()).toBeVisible();
		const file = join(OUT, `${frame.id}-${size.slice(1)}${scheme === 'dark' ? '-dark' : ''}.png`);
		// A long list is taller than the pane's scroll area: capture the whole group, heading to last row.
		if (frame.setup.tall) await group.screenshot({ path: file, animations: 'disabled' });
		else await page.screenshot({ path: file, animations: 'disabled' });
		writeFileSync(join(OUT, 'measure', `${frame.id}-${size.slice(1)}${scheme === 'dark' ? '-dark' : ''}.json`), JSON.stringify(await measureGroup(group), null, 2));
	});
}

for (const [size, dims] of Object.entries(SIZES)) {
	test(`before and neighbour ${size}`, async ({ page }) => {
		await install(page, {});
		await loginByUrl(page, { waitForComposer: false });
		await page.setViewportSize(dims);
		await openSettings(page);
		await openSettingsSection(page, 'System');
		await expect(page.getByRole('region', { name: 'Urgent notifications' })).toBeVisible();
		await page.addStyleTag({ content: 'section[aria-label="Urgent notifications"] { display: none !important; }' });
		await page.getByTestId('devices-row').evaluate((el) => el.scrollIntoView({ block: 'center' }));
		await settle(page);
		await page.screenshot({ path: join(OUT, `before-${size.slice(1)}.png`), animations: 'disabled' });
		await page.getByRole('region', { name: 'Display' }).evaluate((el) => el.scrollIntoView({ block: 'start' }));
		await settle(page);
		await page.screenshot({ path: join(OUT, `neighbour-focus-mode-${size.slice(1)}.png`), animations: 'disabled' });
	});
}

/** Where a tap lands, captured for real: the item opened in Activity by selecting it (the state openActivity produces). */
for (const [size, dims] of Object.entries(SIZES)) {
	test(`tap lands on the item ${size}`, async ({ page, request }) => {
		const thread = await createSession(request, 'Billing');
		const now = Date.now();
		const title = 'Approve purchase order inv-2291, or let it lapse at 17:00?';
		const source = { thread_id: thread, thread_label: 'Billing', project_id: null, project_label: null, parent_reviewed: true };
		const items = [{
			key: 'decision:inv-2291', external_id: 'inv-2291', renderer: 'decision', kind: 'decision', title,
			raised_at: new Date(now - 600_000).toISOString(), source, open: null,
			rank: { position: 1, reason: 'The supplier cancels it at 17:00 today.', ranked_by: thread, ranked_at: new Date(now).toISOString() },
			detail: {
				id: 'decision-inv-2291', plan_id: 'plan-push', node_id: 'node-push', source_node_id: 'node-push-src', thread_id: thread, title,
				context: 'The supplier cancels the order at 17:00 today if it has no approval.', tradeoff: '', recommendation: 'approve', rationale: '',
				other_enabled: false, order_key: 0, revision: 1, raised_at: new Date(now - 600_000).toISOString(), source,
				options: [
					{ id: 'approve', label: 'Approve it', detail: 'The order goes ahead.' },
					{ id: 'lapse', label: 'Let it lapse', detail: 'The supplier cancels it at 17:00.' },
				],
			},
		}];
		await page.route(/\/api\/attention\/queue\?(.*&)?view=activity/, (route) => route.fulfill({
			status: 200, contentType: 'application/json', body: JSON.stringify({ count: 1, items, ranker: { thread_id: thread, label: 'CORSIA' } }),
		}));
		await install(page, {});
		await loginByUrl(page, { waitForComposer: false });
		await page.setViewportSize(dims);
		if (dims.width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
		const row = page.locator('[data-activity-row]').first();
		await expect(row).toBeVisible();
		await row.click();
		await settle(page);
		await expect(page.getByText('Let it lapse', { exact: true }).first()).toBeVisible();
		await page.screenshot({ path: join(OUT, `tap-lands-${size.slice(1)}.png`), animations: 'disabled' });
	});
}

/** Round 1's findings F1, F2, F3, F5 and F10, measured on the running screen. */
for (const [size, dims] of Object.entries(SIZES)) {
	test(`measure on-others ${size}`, async ({ page }) => {
		const { group } = await openGroup(page, { subscribed: true, devices: [...others, dev('d5', 'iPhone', 26), dev('d9', 'Firefox on the shared Windows workstation in the east wing of the second floor office', 19, 'turned_off')] }, dims, 'light');
		await group.evaluate((el) => el.scrollIntoView({ block: 'center' }));
		await settle(page);
		const result = await group.evaluate((section) => {
			const box = (el: Element) => { const r = el.getBoundingClientRect(); return { left: r.left, right: r.right, width: r.width, height: r.height }; };
			const sw = section.querySelector('button[aria-label="Urgent notifications on this device"]') as HTMLElement;
			const hit = sw.querySelector('.hit') as HTMLElement | null;
			const acts = [...section.querySelectorAll('button.act')] as HTMLElement[];
			const glyphRight = (el: HTMLElement) => { const range = document.createRange(); range.selectNodeContents(el); return range.getBoundingClientRect().right; };
			return {
				width: window.innerWidth,
				switchTrack: box(sw),
				switchHit: hit ? box(hit) : null,
				actions: acts.map((a) => ({ name: a.getAttribute('aria-label'), text: a.textContent?.trim(), box: box(a), glyphRight: glyphRight(a), minHeight: getComputedStyle(a).minHeight })),
				names: [...section.querySelectorAll('.name')].map((n) => ({ text: n.textContent, wraps: getComputedStyle(n).whiteSpace !== 'nowrap', clipped: (n as HTMLElement).scrollWidth > (n as HTMLElement).clientWidth })),
				sublines: [...section.querySelectorAll('.sub')].map((n) => n.textContent?.trim()),
			};
		});
		const firstAct = result.actions[0];
		expect(result.switchHit?.width).toBeGreaterThanOrEqual(44);
		expect(result.switchHit?.height).toBeGreaterThanOrEqual(44);
		expect(Math.abs(firstAct.glyphRight - result.switchTrack.right)).toBeLessThanOrEqual(0.6);
		expect(result.actions.every((a) => a.name && a.name !== a.text)).toBe(true);
		expect(new Set(result.actions.map((a) => a.name)).size).toBe(result.actions.length);
		expect(result.actions.every((a) => / added [A-Z][a-z]{2} \d{1,2}$/.test(a.name ?? ''))).toBe(true);
		expect(result.names.some((n) => n.clipped)).toBe(false);
		const shape = await measureGroup(group);
		expect(shape.rows.every((r) => r.nameLines === 1)).toBe(true);
		expect(shape.rows.every((r) => r.subLines <= 1)).toBe(true);
		expect(shape.rows.every((r) => r.subClipped === false)).toBe(true);
		// Keyboard focus ring on a text action.
		await switchOf(page).focus();
		await page.keyboard.press('Tab');
		const ring = await page.evaluate(() => { const el = document.activeElement as HTMLElement; const cs = getComputedStyle(el); return { label: el.getAttribute('aria-label'), focusVisible: el.matches(':focus-visible'), outline: `${cs.outlineStyle} ${cs.outlineWidth} ${cs.outlineColor}`, offset: cs.outlineOffset }; });
		expect(ring.focusVisible).toBe(true);
		expect(ring.outline).toMatch(/^solid 2px /);
		const aria = await group.ariaSnapshot();
		writeFileSync(join(OUT, 'measure', `on-others-hit-and-ring-${size.slice(1)}.json`), JSON.stringify({ ...result, ring, aria, shape }, null, 2));
	});
}

test('Cancel is a way out: the switch goes back to Off and a late answer does nothing', async ({ page }) => {
	const { group, controls } = await openGroup(page, { answer: 'manual' }, SIZES.w393, 'light');
	await tap(page);
	await expect(group.getByText('Waiting for your permission…')).toBeVisible();
	await expect(switchOf(page)).toHaveAttribute('aria-pressed', 'false');
	const order = await group.evaluate((section) => {
		const cancel = section.querySelector('button.cancel') as HTMLElement;
		const sw = section.querySelector('button[aria-label="Urgent notifications on this device"]') as HTMLElement;
		return { domBefore: !!(cancel.compareDocumentPosition(sw) & Node.DOCUMENT_POSITION_FOLLOWING), visualBefore: cancel.getBoundingClientRect().right <= sw.getBoundingClientRect().left };
	});
	expect(order).toEqual({ domBefore: true, visualBefore: true });
	const target = await group.getByRole('button', { name: 'Cancel' }).evaluate((el) => {
		const r = el.getBoundingClientRect();
		const cx = r.left + r.width / 2;
		const at = (dy: number) => document.elementFromPoint(cx, r.top + r.height / 2 + dy)?.closest('button.cancel') !== null;
		return { above: at(-20), below: at(20) };
	});
	expect(target).toEqual({ above: true, below: true });
	await group.getByRole('button', { name: 'Cancel' }).click();
	await expect(group.getByText('Waiting for your permission…')).toHaveCount(0);
	await expect(group.getByRole('button', { name: 'Cancel' })).toHaveCount(0);
	await expect(switchOf(page)).toHaveAttribute('aria-pressed', 'false');
	await page.evaluate(() => (window as unknown as { __answerPrompt: (a: string) => void }).__answerPrompt('granted'));
	await page.waitForTimeout(500);
	expect(controls.puts).toEqual([]);
	await expect(switchOf(page)).toHaveAttribute('aria-pressed', 'false');
});

test('the device list is read again when the page becomes visible', async ({ page }) => {
	const { group, controls } = await openGroup(page, { subscribed: true, devices: [mine('on'), phone] }, SIZES.w393, 'light');
	await expect(group.getByText('Chrome on Mac')).toHaveCount(0);
	controls.setDevices([mine('on'), phone, mac]);
	await page.evaluate(() => document.dispatchEvent(new Event('visibilitychange')));
	await expect(group.getByText('Chrome on Mac')).toBeVisible();
	controls.setDevices([mine('on'), phone]);
	await page.evaluate(() => window.dispatchEvent(new Event('focus')));
	await expect(group.getByText('Chrome on Mac')).toHaveCount(0);
});

for (const scheme of ['light', 'dark'] as const) {
	test(`a running Turn off or Remove reads in one colour, ${scheme}`, async ({ page }) => {
		const { group } = await openGroup(page, { subscribed: true, devices: others, hold: true }, SIZES.w393, scheme);
		await bothOthers(page);
		await expect(group.getByText('Removing\u2026')).toBeVisible();
		const colours = await group.evaluate((section) => {
			const of = (text: string) => { const el = [...section.querySelectorAll('*')].find((n) => n.children.length === 0 && n.textContent?.trim() === text) as HTMLElement; return getComputedStyle(el).color; };
			const on = section.querySelector('.hint') as HTMLElement;
			return { turningOff: of('Turning off\u2026'), removing: of('Removing\u2026'), hintLine: getComputedStyle(on).color };
		});
		expect(colours.removing).toBe(colours.turningOff);
		expect(colours.removing).toBe(colours.hintLine);
		writeFileSync(join(OUT, 'measure', `pending-colour-393-${scheme}.json`), JSON.stringify(colours, null, 2));
	});
}
