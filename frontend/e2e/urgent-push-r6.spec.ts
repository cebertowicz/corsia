import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, type BrowserContext, type Page } from '@playwright/test';
import { test } from './support/test';
import { createSession, TEST_SESSION_TOKEN } from './support/corsia';
import { setDebugVersionOverlay } from './support/heldComments';
import { install } from './contact-sheet/urgent-push';

/**
 * Design round 6 of the urgent notifications design: a tap while the install can't be reached and then reconnects, the
 * address across a real reload during sign-in, and the tap on an open window (uncontrolled and controlled) in real Chromium.
 * Output: R6_OUT (frames/ and measure/).
 */
const OUT = process.env.R6_OUT ?? join(process.cwd(), 'e2e-results', 'urgent-push-r6');
mkdirSync(join(OUT, 'frames'), { recursive: true });
mkdirSync(join(OUT, 'measure'), { recursive: true });
const save = (name: string, data: unknown) => writeFileSync(join(OUT, 'measure', name), JSON.stringify(data, null, 1));
test.beforeEach(async ({ request }) => { await setDebugVersionOverlay(request, false); });

async function frame(page: Page, name: string) {
	await page.mouse.move(0, 0);
	await page.waitForTimeout(450);
	await page.screenshot({ path: join(OUT, 'frames', `${name}.png`), animations: 'disabled' });
}

const TOP = 'Reply to the landlord about the lease renewal?';
const TITLE = 'Approve purchase order inv-2291, or let it lapse at 17:00?';

/** Activity's list: two decisions; the second is the one the notification names. Routed on the context so every page sees it. */
async function activityFixture(context: BrowserContext, request: Parameters<typeof createSession>[0]) {
	const thread = await createSession(request, 'Billing');
	const now = Date.now();
	const source = { thread_id: thread, thread_label: 'Billing', project_id: null, project_label: null, parent_reviewed: true };
	const item = (id: string, t: string, position: number) => ({
		key: `decision:${id}`, external_id: id, renderer: 'decision', kind: 'decision', title: t,
		raised_at: new Date(now - 600_000 * position).toISOString(), source, open: null,
		rank: { position, reason: 'The supplier cancels it at 17:00 today.', ranked_by: thread, ranked_at: new Date(now).toISOString() },
		detail: {
			id: `decision-${id}`, plan_id: 'plan-push', node_id: `node-${id}`, source_node_id: `node-${id}-src`, thread_id: thread, title: t,
			context: 'The supplier cancels the order at 17:00 today if it has no approval.', tradeoff: '', recommendation: 'approve', rationale: '',
			other_enabled: false, order_key: position, revision: 1, raised_at: new Date(now - 600_000 * position).toISOString(), source,
			options: [{ id: 'approve', label: 'Approve it', detail: 'The order goes ahead.' }, { id: 'lapse', label: 'Let it lapse', detail: 'The supplier cancels it at 17:00.' }],
		},
	});
	const items = [item('mail-114', TOP, 1), item('inv-2291', TITLE, 2)];
	await context.route(/\/api\/attention\/queue\?(.*&)?view=activity/, (route) => route.fulfill({
		status: 200, contentType: 'application/json', body: JSON.stringify({ count: items.length, items, ranker: { thread_id: thread, label: 'CORSIA' } }),
	}));
	await context.route('**/api/corsia-agent*', async (route) => {
		const response = await route.fetch();
		await route.fulfill({ response, json: { ...(await response.json()), first_run_pending: false } });
	});
}
const signedIn = (context: BrowserContext) => context.addInitScript((t) => { try { localStorage.setItem('corsia_token', t); } catch { /* none */ } }, TEST_SESSION_TOKEN);
const selected = (page: Page) => page.locator('[data-activity-row][aria-current="true"]');

// ---- J2: the tap while the install can't be reached, then reachable (R9 and the conductor's added condition) ---------
/** Opens the tapped address while every /api call is refused and the socket closes; returns the switch that brings the install back. */
async function tapWhileUnreachable(page: Page, request: Parameters<typeof createSession>[0], out: Record<string, unknown>, tag: string) {
	await activityFixture(page.context(), request);
	await install(page, {});
	await signedIn(page.context());
	await page.setViewportSize({ width: 393, height: 852 });
	const state = { down: true };
	await page.route((url) => url.pathname.startsWith('/api/'), async (route) => { if (state.down) await route.abort('connectionrefused'); else await route.fallback(); });
	await page.routeWebSocket((url) => url.pathname.includes('/ws'), (ws) => { if (state.down) void ws.close(); else ws.connectToServer(); });
	await page.goto('/?activity=inv-2291');
	await expect(page.getByRole('heading', { name: 'CORSIA couldn’t start' })).toBeVisible({ timeout: 45000 });
	out.unreachable = { url: page.url(), stored: await page.evaluate(() => sessionStorage.getItem('corsia_activity_address')), body: (await page.locator('body').innerText()).slice(0, 300) };
	await frame(page, `${tag}-unreachable-393`);
	return state;
}
async function readOpened(page: Page, out: Record<string, unknown>) {
	await page.waitForTimeout(800);
	out.urlAfter = page.url();
	out.selectedRows = await selected(page).count();
	out.selectedText = (await selected(page).first().innerText().catch(() => '')).slice(0, 120);
	out.topItemSelected = (await selected(page).filter({ hasText: 'landlord' }).count()) > 0;
	out.namedItemVisible = (await page.getByText(TITLE).and(page.locator(':visible')).count()) > 0;
	out.storedAfterOpen = await page.evaluate(() => sessionStorage.getItem('corsia_activity_address'));
}

test('J2 tap while unreachable, then a reload once it is reachable: the named item opens (393)', async ({ page, request }) => {
	test.setTimeout(240_000);
	const out: Record<string, unknown> = {};
	const state = await tapWhileUnreachable(page, request, out, 'J2');
	state.down = false;
	await page.reload();
	await expect(page.getByText('Let it lapse', { exact: true }).first()).toBeVisible({ timeout: 90000 });
	await readOpened(page, out);
	await frame(page, 'J2-connected-393');
	save('J2-unreachable-then-reload-393.json', out);
	expect(String(out.urlAfter)).not.toContain('activity=');
	expect(String(out.selectedText)).toContain('inv-2291');
	expect(out.topItemSelected).toBe(false);
	expect(out.storedAfterOpen).toBeNull();
});

test('J2b tap while unreachable, then Retry once it is reachable: what the app does (393)', async ({ page, request }) => {
	test.setTimeout(240_000);
	const out: Record<string, unknown> = {};
	const state = await tapWhileUnreachable(page, request, out, 'J2b');
	let loginAnswered = false;
	await page.route('**/api/auth/login', async (route) => {
		loginAnswered = true;
		await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ token: TEST_SESSION_TOKEN, user: { id: 'u', name: 'Test' } }) });
	});
	state.down = false;
	await page.getByRole('button', { name: 'Retry' }).click();
	await page.waitForTimeout(2500);
	out.afterRetry = { signInForm: await page.getByPlaceholder('Password').isVisible().catch(() => false), openedWithoutSignIn: (await selected(page).count()) > 0 };
	await frame(page, 'J2b-after-retry-393');
	if (out.afterRetry && (out.afterRetry as { signInForm: boolean }).signInForm) {
		await page.getByPlaceholder('Username').fill('steve');
		await page.getByPlaceholder('Password').fill('not-a-real-password');
		await page.getByRole('button', { name: 'Sign in' }).click();
	}
	await expect(page.getByText('Let it lapse', { exact: true }).first()).toBeVisible({ timeout: 90000 });
	await readOpened(page, out);
	out.loginAnswered = loginAnswered;
	await frame(page, 'J2b-connected-393');
	save('J2b-unreachable-then-retry-393.json', out);
	expect(String(out.selectedText)).toContain('inv-2291');
	expect(out.topItemSelected).toBe(false);
});

// ---- R9: the address survives a real reload during sign-in --------------------------------------------------------
test('R9 a real reload during sign-in keeps the address (393)', async ({ page, request }) => {
	test.setTimeout(240_000);
	const out: Record<string, unknown> = {};
	await activityFixture(page.context(), request);
	await install(page, {});
	await page.setViewportSize({ width: 393, height: 852 });
	let loginAnswered = false;
	await page.route('**/api/auth/login', async (route) => {
		loginAnswered = true;
		await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ token: TEST_SESSION_TOKEN, user: { id: 'u', name: 'Test' } }) });
	});
	await page.route('**/api/auth/status', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ has_users: true }) }));
	await page.addInitScript(() => { try { localStorage.removeItem('corsia_token'); } catch { /* none */ } });
	page.on('console', (m) => { if (m.type() === 'error' || m.type() === 'warning') console.log('PAGE', m.text().slice(0, 300)); });
	page.on('pageerror', (e) => console.log('PAGEERROR', String(e).slice(0, 300)));
	page.on('requestfailed', (r) => console.log('REQFAILED', r.url().slice(0, 150)));
	await page.goto('/?activity=inv-2291');
	await expect(page.getByPlaceholder('Password')).toBeVisible({ timeout: 45000 });
	out.urlBeforeReload = page.url();
	out.storedBeforeReload = await page.evaluate(() => sessionStorage.getItem('corsia_activity_address'));
	await page.reload();
	await expect(page.getByPlaceholder('Password')).toBeVisible({ timeout: 45000 });
	out.urlAfterReload = page.url();
	out.storedAfterReload = await page.evaluate(() => sessionStorage.getItem('corsia_activity_address'));
	await page.getByPlaceholder('Username').fill('steve');
	await page.getByPlaceholder('Password').fill('not-a-real-password');
	await page.getByRole('button', { name: 'Sign in' }).click();
	await expect(page.getByText('Let it lapse', { exact: true }).first()).toBeVisible({ timeout: 60000 });
	await page.waitForTimeout(800);
	out.loginAnswered = loginAnswered;
	out.selectedText = (await selected(page).first().innerText().catch(() => '')).slice(0, 120);
	out.storedAfterOpen = await page.evaluate(() => sessionStorage.getItem('corsia_activity_address'));
	await frame(page, 'R9-reload-signin-after-393');
	save('R9-reload-signin-393.json', out);
	expect(out.storedBeforeReload).toBe('inv-2291');
	expect(out.storedAfterReload).toBe('inv-2291');
	expect(String(out.selectedText)).toContain('inv-2291');
	expect(out.storedAfterOpen).toBeNull();
});

// ---- R17/R9: the tap on an open window, uncontrolled and controlled -------------------------------------------------
/**
 * The real push worker (`/push-sw.js`), registered in real Chromium. A notification click cannot be fired from outside the
 * browser, and this Chromium refuses showNotification in a worker, so the test dispatches a `notificationclick` event carrying a
 * stand-in notification from inside the worker: the same handler function runs, on a real window client list. (`focus()` refuses a synthetic event
 * with no user interaction; the worker still hands the window the path, which is what the test reads.)
 */
async function click(context: BrowserContext, path: string) {
	const [worker] = context.serviceWorkers().filter((w) => w.url().endsWith('/push-sw.js'));
	return worker.evaluate(async (p) => {
		// Headless Chromium refuses showNotification inside a worker even with the permission granted to the page, so the
		// event carries a stand-in for the notification: the handler reads only close() and data.path from it.
		const event = new ExtendableEvent('notificationclick') as ExtendableEvent & { notification?: unknown };
		event.notification = { close: () => undefined, data: { path: p }, tag: p };
		self.dispatchEvent(event);
		await new Promise((resolve) => setTimeout(resolve, 600));
		return true;
	}, path);
}

test('R17 a tap on a window opened before the worker registered, and on one opened after (393)', async ({ page, request, context }) => {
	test.setTimeout(300_000);
	const out: Record<string, unknown> = {};
	await activityFixture(context, request);
	await signedIn(context);
	await page.setViewportSize({ width: 393, height: 852 });
	// Window A: opened first, so the worker never controls it.
	await page.goto('/');
	await expect(page.getByRole('button', { name: /new chat|chat/i }).first()).toBeVisible({ timeout: 60000 }).catch(() => undefined);
	await page.waitForTimeout(2500);
	out.aUrlBefore = page.url();
	await page.evaluate(async () => { const r = await navigator.serviceWorker.register('/push-sw.js', { scope: '/' }); await navigator.serviceWorker.ready; return r.scope; });
	await expect.poll(() => context.serviceWorkers().filter((w) => w.url().endsWith('/push-sw.js')).length, { timeout: 30000 }).toBeGreaterThan(0);
	out.aControlled = await page.evaluate(() => navigator.serviceWorker.controller !== null);
	// The worker's own matchAll lists it only with includeUncontrolled: the tap must still reach it.
	await click(context, '/?activity=inv-2291');
	await expect(page.getByText('Let it lapse', { exact: true }).first()).toBeVisible({ timeout: 30000 });
	await page.waitForTimeout(600);
	out.aSelected = (await selected(page).first().innerText().catch(() => '')).slice(0, 120);
	out.aUrl = page.url();
	await frame(page, 'R17-tap-uncontrolled-393');
	// Window B: opened after the worker is active, so it is controlled once it loads.
	await page.close();
	const b = await context.newPage();
	await b.setViewportSize({ width: 393, height: 852 });
	await b.goto('/');
	await b.waitForTimeout(2500);
	await b.evaluate(() => navigator.serviceWorker.ready);
	await b.reload();
	await b.waitForTimeout(2500);
	out.bControlled = await b.evaluate(() => navigator.serviceWorker.controller !== null);
	await click(context, '/?activity=mail-114');
	await expect(b.getByText('Reply to the landlord about the lease renewal?').and(b.locator(':visible')).first()).toBeVisible({ timeout: 30000 });
	await b.waitForTimeout(600);
	out.bSelected = (await selected(b).first().innerText().catch(() => '')).slice(0, 120);
	out.bUrl = b.url();
	await frame(b, 'R17-tap-controlled-393');
	save('R17-tap-open-window-393.json', out);
	expect(out.aControlled).toBe(false);
	expect(String(out.aSelected)).toContain('inv-2291');
	expect(String(out.bSelected)).toContain('landlord');
	expect(String(out.aUrl) + String(out.bUrl)).not.toContain('activity=');
});
