import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, type Locator, type Page } from '@playwright/test';
import { test } from './support/test';
import { createSession, loginByUrl, TEST_SESSION_TOKEN } from './support/corsia';
import { setDebugVersionOverlay } from './support/heldComments';
import { ENDPOINT_ID, install, openState, setups } from './contact-sheet/urgent-push';

/**
 * Design round 5 of the urgent notifications design: the gone item, the tap through sign-in and through connecting, and
 * the closing review's fixes F1-F9, each rendered and measured. Output: R5_OUT (frames/ and measure/).
 */
const OUT = process.env.R5_OUT ?? join(process.cwd(), 'e2e-results', 'urgent-push-r5');
mkdirSync(join(OUT, 'frames'), { recursive: true });
mkdirSync(join(OUT, 'measure'), { recursive: true });
const save = (name: string, data: unknown) => writeFileSync(join(OUT, 'measure', name), JSON.stringify(data, null, 1));
const group = (page: Page) => page.getByRole('region', { name: 'Urgent notifications' });
const HEIGHT = { 380: 852, 393: 852, 1440: 1000 } as const;

test.beforeEach(async ({ request }) => { await setDebugVersionOverlay(request, false); });

async function frame(page: Page, name: string, locator?: Locator) {
	await page.mouse.move(0, 0);
	await page.waitForTimeout(450);
	await (locator ?? page).screenshot({ path: join(OUT, 'frames', `${name}.png`), animations: 'disabled' });
}
const active = (page: Page) => page.evaluate(() => {
	const el = document.activeElement as HTMLElement | null;
	if (!el || el === document.body) return 'body (focus lost)';
	return `${el.tagName.toLowerCase()}: ${el.getAttribute('aria-label') ?? el.textContent?.trim()} | focus-visible=${el.matches(':focus-visible')} | inGroup=${!!el.closest('section[aria-label="Urgent notifications"]')}`;
});

// ---- Activity fixture: two decisions, the second is the one a notification names. ----------------------------------
const TITLE = 'Approve purchase order inv-2291, or let it lapse at 17:00?';
async function activityFixture(page: Page, request: Parameters<typeof createSession>[0]) {
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
	const items = [item('mail-114', 'Reply to the landlord about the lease renewal?', 1), item('inv-2291', TITLE, 2)];
	await page.route(/\/api\/attention\/queue\?(.*&)?view=activity/, (route) => route.fulfill({
		status: 200, contentType: 'application/json', body: JSON.stringify({ count: items.length, items, ranker: { thread_id: thread, label: 'CORSIA' } }),
	}));
}
/** The fresh instance is still in its first-run landing, which opens the CORSIA thread on every load; a person tapping a notification is past it. */
async function pastFirstRun(page: Page) {
	await page.route('**/api/corsia-agent*', async (route) => {
		const response = await route.fetch();
		const body = await response.json();
		await route.fulfill({ response, json: { ...body, first_run_pending: false } });
	});
}
const GONE_LINE = 'That’s no longer waiting on you.';

// J2: a tap on an item that no longer exists.
for (const width of [393, 1440] as const) {
	test(`J2 gone item ${width}`, async ({ page, request }) => {
		test.setTimeout(180_000);
		const out: Record<string, unknown> = {};
		await activityFixture(page, request);
		await install(page, {});
		await loginByUrl(page, { waitForComposer: false });
		await pastFirstRun(page);
		await page.setViewportSize({ width, height: HEIGHT[width] });
		await page.goto('/?activity=answered-long-ago');
		await expect(page.getByText(GONE_LINE)).toBeVisible({ timeout: 45000 });
		await page.waitForTimeout(800);
		out.notice = await page.getByText(GONE_LINE).evaluate((el) => ({ role: el.getAttribute('role'), text: el.textContent, lines: new Set([...(() => { const r = document.createRange(); r.selectNodeContents(el); return [...r.getClientRects()].map((x) => Math.round(x.top)); })()]).size }));
		out.selectedRows = await page.locator('[data-activity-row][aria-current="true"]').count();
		out.rowsListed = await page.locator('[data-activity-row]').count();
		out.paneLabel = await page.locator('.pane-col').getAttribute('aria-label');
		out.paneText = (await page.locator('.pane-col').innerText()).trim();
		out.addressCleaned = !new URL(page.url()).searchParams.has('activity');
		await frame(page, `J2-gone-${width}`);
		// After a minute of waiting nothing selected itself; opening a row lets the line go.
		await page.waitForTimeout(2500);
		out.selectedRowsAfterWait = await page.locator('[data-activity-row][aria-current="true"]').count();
		await page.locator('[data-activity-row]').first().click();
		await expect(page.getByText(GONE_LINE)).toHaveCount(0);
		out.noticeAfterOpeningRow = await page.getByText(GONE_LINE).count();
		save(`J2-gone-${width}.json`, out);
		expect(out.selectedRows).toBe(0);
		expect(out.selectedRowsAfterWait).toBe(0);
		expect(out.rowsListed).toBe(2);
		expect(out.addressCleaned).toBe(true);
		expect((out.notice as { lines: number }).lines).toBe(1);
		if (width === 1440) { expect(out.paneLabel).toBe('Nothing open'); expect(out.paneText).toBe('Choose one to open it.'); }
	});
}

// J2 / R9: the address survives sign-in. The sign-in form is the app's own; only its answer is faked, with the test session.
test('R9 tap through sign-in 393', async ({ page, request }) => {
	test.setTimeout(180_000);
	const out: Record<string, unknown> = {};
	await activityFixture(page, request);
	await install(page, {});
	await pastFirstRun(page);
	await page.setViewportSize({ width: 393, height: 852 });
	let loginAnswered = false;
	await page.route('**/api/auth/login', async (route) => {
		loginAnswered = true;
		await route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ token: TEST_SESSION_TOKEN, user: { id: 'u', name: 'Test' } }) });
	});
	await page.route('**/api/auth/status', (route) => route.fulfill({ status: 200, contentType: 'application/json', body: JSON.stringify({ has_users: true }) }));
	// Signed out: nothing stored in this browser.
	await page.addInitScript(() => { try { localStorage.removeItem('corsia_token'); } catch { /* none */ } });
	await page.goto('/?activity=inv-2291');
	await expect(page.getByPlaceholder('Password')).toBeVisible({ timeout: 45000 });
	out.signedOutUrl = page.url();
	await page.waitForTimeout(500);
	await frame(page, 'R9-signin-1-form-393');
	await page.getByPlaceholder('Username').fill('steve');
	await page.getByPlaceholder('Password').fill('not-a-real-password');
	await page.getByRole('button', { name: 'Sign in' }).click();
	await expect(page.getByText('Let it lapse', { exact: true }).first()).toBeVisible({ timeout: 60000 });
	await page.waitForTimeout(800);
	out.loginAnswered = loginAnswered;
	out.urlAfter = page.url();
	out.landedOnNamedItem = (await page.getByText(TITLE).and(page.locator(':visible')).count()) > 0;
	out.topItemShownInstead = (await page.getByText('Reply to the landlord about the lease renewal?').and(page.locator(':visible')).count()) > 0;
	await frame(page, 'R9-signin-2-after-393');
	save('R9-signin-393.json', out);
	expect(out.loginAnswered).toBe(true);
	expect(out.landedOnNamedItem).toBe(true);
});

// J2: an install still connecting. Every request fails until it is reachable; the app shows its own connection state, then the item.
test('J2 tap while connecting 393', async ({ page, request }) => {
	test.setTimeout(240_000);
	const out: Record<string, unknown> = {};
	await activityFixture(page, request);
	await install(page, {});
	await pastFirstRun(page);
	await page.setViewportSize({ width: 393, height: 852 });
	await page.addInitScript((t) => { try { localStorage.setItem('corsia_token', t); } catch { /* none */ } }, TEST_SESSION_TOKEN);
	// The install answers nothing until it is reachable: every request waits, then goes through.
	let release: () => void = () => {};
	const gate = new Promise<void>((resolve) => { release = resolve; });
	await page.route('**/api/**', async (route, req) => {
		if (!req.url().includes('/api/push')) await gate;
		await route.fallback();
	});
	await page.goto('/?activity=inv-2291');
	await page.waitForTimeout(4000);
	out.whileUnreachable = { url: page.url(), body: (await page.locator('body').innerText()).slice(0, 400) };
	await frame(page, 'J2-connecting-1-unreachable-393');
	release();
	const retry = page.getByRole('button', { name: 'Retry' });
	if (await retry.isVisible().catch(() => false)) await retry.click();
	await expect(page.getByText('Let it lapse', { exact: true }).first()).toBeVisible({ timeout: 90000 });
	await page.waitForTimeout(800);
	out.landedOnNamedItem = (await page.getByText(TITLE).and(page.locator(':visible')).count()) > 0;
	await frame(page, 'J2-connecting-2-connected-393');
	save('J2-connecting-393.json', out);
	expect(out.landedOnNamedItem).toBe(true);
});

// ---- F1: 44 px between the action and Cancel ------------------------------------------------------------------------
test('F1 confirm gap', async ({ page }) => {
	test.setTimeout(180_000);
	const out: Record<string, unknown> = {};
	for (const width of [380, 393, 1440] as const) {
		for (const ask of ['confirm-turn-off', 'confirm-remove'] as const) {
			await page.setViewportSize({ width, height: HEIGHT[width] });
			await openState(page, ask);
			await group(page).evaluate((el) => el.scrollIntoView({ block: 'center' }));
			out[`${ask}-${width}`] = await group(page).evaluate((section) => {
				const open = [...section.querySelectorAll('.actions.confirming')][0] as HTMLElement;
				const [action, cancel] = [...open.querySelectorAll('button')] as HTMLElement[];
				const hit = (el: HTMLElement) => { const r = el.getBoundingClientRect(); const a = getComputedStyle(el, '::after'); const v = a.content !== 'none' && a.position === 'absolute' ? [parseFloat(a.left), parseFloat(a.right)] : [0, 0]; return { left: r.left + v[0], right: r.right - v[1] }; };
				const [a, c] = [hit(action), hit(cancel)];
				const row = open.closest('.settings-row')!.getBoundingClientRect();
				return { action: action.textContent?.trim(), cancel: cancel.textContent?.trim(), boxGapPx: Math.round((cancel.getBoundingClientRect().left - action.getBoundingClientRect().right) * 10) / 10, targetGapPx: Math.round((c.left - a.right) * 10) / 10, rowRight: Math.round(row.right), cancelRight: Math.round(cancel.getBoundingClientRect().right), rowHeight: Math.round(row.height) };
			});
			if (width !== 1440) await frame(page, `F1-${ask}-${width}`, group(page));
		}
	}
	save('F1-gap.json', out);
	for (const [k, v] of Object.entries(out)) expect((v as { boxGapPx: number }).boxGapPx, k).toBeGreaterThanOrEqual(44);
});

// ---- F2: Escape; F3: the row leaves mid-confirm ---------------------------------------------------------------------
test('F2 Escape and F3 row leaves mid-confirm', async ({ page }) => {
	test.setTimeout(180_000);
	const out: Record<string, unknown> = {};
	await page.setViewportSize({ width: 393, height: 852 });
	await openState(page, 'on-others');
	await group(page).scrollIntoViewIfNeeded();
	const turnOff = group(page).getByRole('button', { name: /^Turn off urgent notifications on iPhone/ });
	await turnOff.focus();
	await page.keyboard.press('Enter');
	await page.waitForTimeout(500);
	out.afterOpen = await active(page);
	await frame(page, 'F2-confirm-open-393', group(page));
	await page.keyboard.press('Escape');
	await page.waitForTimeout(500);
	out.afterEscape = await active(page);
	await page.waitForTimeout(700);
	out.confirmOpenAfterEscape = (await group(page).getByText('Turn off urgent notifications on iPhone?').evaluateAll((els) => els.some((e) => !e.closest('[inert]'))));
	out.escapeMatch = await group(page).getByText('Turn off urgent notifications on iPhone?').evaluateAll((els) => els.map((e) => e.parentElement!.outerHTML.slice(0, 300) + ' | ' + getComputedStyle(e.parentElement!).display + ' ' + getComputedStyle(e.parentElement!).visibility + ' ' + e.parentElement!.getBoundingClientRect().height));
	await frame(page, 'F2-after-escape-393', group(page));
	// Escape also works from the confirm button, and the page's own Escape handling is not triggered twice.
	await turnOff.focus();
	await page.keyboard.press('Enter');
	await page.waitForTimeout(400);
	await page.keyboard.press('Shift+Tab');
	out.onConfirmButton = await active(page);
	await page.keyboard.press('Escape');
	await page.waitForTimeout(400);
	out.afterEscapeFromConfirmButton = await active(page);
	// F3: the iPhone row leaves (another device turned it off) while its confirm is open.
	await page.keyboard.press('Enter');
	await page.waitForTimeout(450);
	out.confirmOpenAgain = (await group(page).getByText('Turn off urgent notifications on iPhone?').evaluateAll((els) => els.some((e) => !e.closest('[inert]'))));
	await page.route('**/api/push', (route) => route.fulfill({ json: { public_key: 'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U', devices: [
		{ id: 'this', name: 'This browser', state: 'on', endpoint_id: ENDPOINT_ID },
		{ id: 'm1', name: 'Chrome on Mac', state: 'turned_off', endpoint_id: 'm1', added_at: '2026-11-22T15:00:00Z' },
	] } }));
	await page.evaluate(() => { window.dispatchEvent(new Event('focus')); document.dispatchEvent(new Event('visibilitychange')); });
	await page.waitForTimeout(900);
	out.afterRowLeft = await active(page);
	out.iphoneRowStillListed = await group(page).getByText('iPhone', { exact: true }).count();
	await frame(page, 'F3-row-left-mid-confirm-393', group(page));
	// The last row leaves while confirming: focus goes to the switch.
	await page.keyboard.press('Enter');
	await page.waitForTimeout(450);
	await page.route('**/api/push', (route) => route.fulfill({ json: { public_key: 'BEl62iUYgUivxIkv69yViEuiBIa-Ib9-SkvMeAtA3LFgDzkrxZJjSgSnfckjBJuBkr3qBUYIHBQFLXYp5Nksh8U', devices: [
		{ id: 'this', name: 'This browser', state: 'on', endpoint_id: ENDPOINT_ID },
	] } }));
	await page.evaluate(() => { window.dispatchEvent(new Event('focus')); document.dispatchEvent(new Event('visibilitychange')); });
	await page.waitForTimeout(900);
	out.afterLastRowLeft = await active(page);
	await frame(page, 'F3-last-row-left-mid-confirm-393', group(page));
	save('F2-F3-keyboard.json', out);
	expect(out.afterEscape).toContain('Turn off urgent notifications on iPhone');
	expect(out.confirmOpenAfterEscape).toBe(false);
	expect(String(out.afterEscapeFromConfirmButton)).toContain('Turn off urgent notifications on iPhone');
	expect(String(out.afterRowLeft)).toContain('Remove Chrome on Mac');
	expect(String(out.afterLastRowLeft)).toContain('Urgent notifications on this device');
});

// ---- F4: the text actions' focus ring, 3:1 against what it sits on, both themes ------------------------------------
test('F4 focus ring contrast', async ({ page }) => {
	test.setTimeout(180_000);
	const out: Record<string, unknown> = {};
	await page.setViewportSize({ width: 393, height: 852 });
	await openState(page, 'on-others');
	for (const theme of ['light', 'dark'] as const) {
		await page.emulateMedia({ colorScheme: theme });
		await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
		await group(page).scrollIntoViewIfNeeded();
		const read = async () => page.evaluate(() => {
			const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
			const ctx = canvas.getContext('2d', { willReadFrequently: true })!;
			const rgba = (c: string) => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = '#000'; ctx.fillStyle = c; ctx.fillRect(0, 0, 1, 1); const d = ctx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
			const lum = ([r, g, b]: number[]) => { const f = (v: number) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
			const bgOf = (el: HTMLElement) => { let base = rgba(getComputedStyle(document.body).backgroundColor).slice(0, 3); const layers: number[][] = []; for (let e: HTMLElement | null = el; e; e = e.parentElement) { const c = rgba(getComputedStyle(e).backgroundColor); if (c[3] > 0) layers.push(c); if (c[3] >= 1) break; } for (const c of layers.reverse()) base = base.map((v, i) => c[i] * c[3] + v * (1 - c[3])); return base; };
			const el = document.activeElement as HTMLElement;
			const cs = getComputedStyle(el);
			const ring = rgba(cs.outlineColor); const bg = bgOf(el.parentElement as HTMLElement);
			const ratio = (Math.max(lum(ring), lum(bg)) + 0.05) / (Math.min(lum(ring), lum(bg)) + 0.05);
			return { label: el.getAttribute('aria-label') ?? el.textContent?.trim(), focusVisible: el.matches(':focus-visible'), outline: `${cs.outlineStyle} ${cs.outlineWidth} ${cs.outlineColor}`, offset: cs.outlineOffset, ratioAgainstBackground: Math.round(ratio * 100) / 100 };
		});
		const sw = group(page).getByRole('button', { name: 'Urgent notifications on this device' });
		await sw.focus();
		await page.keyboard.press('Shift+Tab'); await page.keyboard.press('Tab'); // keyboard focus, so :focus-visible matches
		await page.waitForTimeout(600); // the outline eases in; read it settled
		const sw1 = await read();
		await page.keyboard.press('Tab');
		await page.waitForTimeout(600);
		const act = await read();
		out[theme] = { switch: sw1, textAction: act };
		save('F4-ring.json', out);
		await frame(page, `F4-focus-text-action-${theme}-393`, group(page));
		await page.keyboard.press('Shift+Tab');
		await frame(page, `F4-focus-switch-${theme}-393`, group(page));
		expect((act as { ratioAgainstBackground: number }).ratioAgainstBackground, `${theme} text action ring`).toBeGreaterThanOrEqual(3);
		expect((act as { outline: string }).outline).toBe((sw1 as { outline: string }).outline);
	}
	save('F4-ring.json', out);
});

// ---- F5: the hint, one string, at most two lines at 380 and 393 in every state ---------------------------------------
test('F5 hint lines in every state', async ({ page: first }) => {
	let page = first;
	// Init scripts accumulate on one page (an iPhone Safari state would stay on for later states), so each state gets its own page.
	const fresh = async () => { if (page !== first) await page.close(); page = await first.context().newPage(); };
	test.setTimeout(900_000);
	const out: Record<string, unknown> = {};
	const texts = new Set<string>();
	for (const state of Object.keys(setups)) {
		await fresh();
		await page.setViewportSize({ width: 393, height: 852 });
		await openState(page, state);
		for (const width of [380, 393, 1440] as const) {
			await page.setViewportSize({ width, height: HEIGHT[width] });
			await page.waitForTimeout(250);
			const hint = group(page).locator('.hint');
			const m = await hint.evaluate((el) => {
				const rects: DOMRect[] = [];
				const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
				for (let n = walker.nextNode(); n; n = walker.nextNode()) { const r = document.createRange(); r.selectNodeContents(n); rects.push(...r.getClientRects()); }
				return { text: el.textContent!.trim(), lines: new Set(rects.map((r) => Math.round(r.top))).size };
			});
			texts.add(m.text);
			out[`${state}-${width}`] = m.lines;
			if (state === 'off' && width !== 1440) await frame(page, `F5-hint-off-${width}`, group(page));
		}
	}
	out.distinctHintStrings = [...texts];
	save('F5-hint.json', out);
	expect(texts.size).toBe(1);
	for (const [k, v] of Object.entries(out)) if (/-(380|393)$/.test(k)) expect(v as number, k).toBeLessThanOrEqual(2);
});

// ---- F6-F9 and the wording: strings, the word Off, date-only, no "push" ---------------------------------------------
test('F6-F9 strings and the word Off; no "push" anywhere the person reads', async ({ page: first }) => {
	let page = first;
	// Init scripts accumulate on one page (an iPhone Safari state would stay on for later states), so each state gets its own page.
	const fresh = async () => { if (page !== first) await page.close(); page = await first.context().newPage(); };
	test.setTimeout(900_000);
	const out: Record<string, unknown> = {};
	const everything: string[] = [];
	const read = async (state: string, width = 393) => {
		await fresh();
		await page.setViewportSize({ width, height: 852 });
		await openState(page, state);
		return group(page).evaluate((section) => ({
			text: (section as HTMLElement).innerText,
			labels: [...section.querySelectorAll('[aria-label], [title]')].map((e) => `${e.getAttribute('aria-label') ?? ''} | ${e.getAttribute('title') ?? ''}`),
			offWord: [...section.querySelectorAll('.off')].map((e) => ({ text: e.textContent, color: getComputedStyle(e).color, secondary: getComputedStyle(document.documentElement).getPropertyValue('--text-secondary').trim() })),
			sw: !!section.querySelector('[role="switch"], button[aria-label="Urgent notifications on this device"]'),
		}));
	};
	for (const state of Object.keys(setups)) {
		const r = await read(state);
		everything.push(r.text, ...r.labels);
		out[state] = { lines: r.text.split('\n').filter(Boolean), off: r.offWord, switchPresent: r.sw };
		if (['blocked-browser', 'blocked-phone', 'safari-tab', 'desktop-app', 'unsupported-browser'].includes(state)) {
			await group(page).evaluate((el) => el.scrollIntoView({ block: 'center' }));
			await frame(page, `F9-off-word-${state}-393`, group(page));
			expect(r.offWord.length, `${state}: the word Off where the switch would be`).toBe(1);
			expect(r.offWord[0].text).toBe('Off');
		}
		if (['off', 'on', 'waiting', 'on-others', 'turned-off', 'pending-this'].includes(state)) expect(r.offWord.length, `${state} has its switch`).toBe(0);
	}
	// Failures on this device (F6), stopped and the other-device lines (F7), and the date format (F8).
	const failure = await read('failure-this');
	out.F6 = failure.text.split('\n').filter((l) => /Couldn/.test(l));
	const stopped = await read('turned-off');
	out.F7thisDevice = stopped.text.split('\n').filter((l) => /Stopped/.test(l));
	const others = await read('on-others');
	out.F7F8others = others.text.split('\n').filter(Boolean);
	await group(page).getByRole('button', { name: /^Remove Chrome on Mac/ }).click();
	out.F7confirmRemove = await group(page).getByRole('group', { name: 'Remove Chrome on Mac?' }).innerText();
	await group(page).getByRole('button', { name: 'Cancel' }).click();
	await group(page).getByRole('button', { name: /^Turn off urgent notifications on iPhone/ }).click();
	out.F7confirmTurnOff = await group(page).getByRole('group', { name: 'Turn off urgent notifications on iPhone?' }).innerText();
	await page.keyboard.press('Escape');
	// Dates: one added today and one added last year are calendar dates, never relative.
	const today = new Date(); today.setHours(9, 0, 0, 0);
	const lastYear = new Date(today); lastYear.setFullYear(today.getFullYear() - 1);
	setups['dates'] = { subscribed: true, devices: [
		{ id: 'this', name: 'This browser', state: 'on', endpoint_id: ENDPOINT_ID },
		{ id: 'a', name: 'iPhone', state: 'on', endpoint_id: 'a', added_at: today.toISOString() },
		{ id: 'b', name: 'iPhone', state: 'on', endpoint_id: 'b', added_at: new Date(today.getTime() - 86400000).toISOString() },
		{ id: 'c', name: 'iPad', state: 'turned_off', endpoint_id: 'c', added_at: lastYear.toISOString() },
	] } as never;
	const dates = await read('dates');
	out.F8dates = dates.text.split('\n').filter((l) => /Added/.test(l));
	everything.push(dates.text, ...dates.labels);
	await group(page).evaluate((el) => el.scrollIntoView({ block: 'center' }));
	await frame(page, 'F8-dates-393', group(page));
	const joined = everything.join('\n');
	out.pushWordHits = joined.match(/push[a-z ]*/gi) ?? [];
	save('F6-F9-strings.json', out);
	expect(out.pushWordHits).toEqual([]);
	for (const line of out.F8dates as string[]) expect(line).not.toMatch(/today|yesterday|ago| at \d/i);
	expect((out.F8dates as string[]).join(' ')).toMatch(/Added [A-Z][a-z]{2} \d{1,2}(, \d{4})?/);
	expect(out.F6).toEqual(['Couldn’t turn off urgent notifications on this device. Try again.']);
	expect(String(out.F7thisDevice)).toBe('Stopped on its own. Turn on.');
	expect(joined).not.toMatch(/\bagain\b.*from that device|Turn on again|turn them on again/);
});
