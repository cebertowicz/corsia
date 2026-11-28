import { mkdirSync, writeFileSync } from 'node:fs';
import { join } from 'node:path';
import { expect, type Locator, type Page } from '@playwright/test';
import { test } from './support/test';
import { createSession, loginByUrl } from './support/corsia';
import { setDebugVersionOverlay } from './support/heldComments';
import { install, openState, setups } from './contact-sheet/urgent-push';

/**
 * Round 4 of the urgent push design: the measurements and frames for each named fix (F1-F10, F12), and the sweep over
 * every state × width × theme (clipping, line counts, touch targets, contrast). Output: R4_OUT.
 */
const OUT = process.env.R4_OUT ?? join(process.cwd(), 'e2e-results', 'urgent-push-r4');
mkdirSync(join(OUT, 'frames'), { recursive: true });
mkdirSync(join(OUT, 'measure'), { recursive: true });
const save = (name: string, data: unknown) => writeFileSync(join(OUT, 'measure', name), JSON.stringify(data, null, 1));
const GROUP = 'section[aria-label="Urgent notifications"]';
const SIZES = { 380: 852, 393: 852, 1440: 1000 } as const;

test.beforeEach(async ({ request }) => { await setDebugVersionOverlay(request, false); });

const group = (page: Page) => page.getByRole('region', { name: 'Urgent notifications' });
const active = (page: Page) => page.evaluate(() => {
	const el = document.activeElement as HTMLElement | null;
	if (!el || el === document.body) return 'body (focus lost)';
	return `${el.tagName.toLowerCase()}: ${el.getAttribute('aria-label') ?? el.textContent?.trim()} | focus-visible=${el.matches(':focus-visible')} | inGroup=${!!el.closest('section[aria-label="Urgent notifications"]')}`;
});
async function frame(page: Page, name: string, locator?: Locator) {
	await page.mouse.move(0, 0);
	await page.waitForTimeout(450);
	await (locator ?? page).screenshot({ path: join(OUT, 'frames', `${name}.png`), animations: 'disabled' });
}
async function focusBefore(page: Page, name: string) {
	// Put keyboard focus on the first control of the group above, so Tab walks the group in its own order.
	await page.evaluate(() => (document.querySelector('[data-testid="devices-row"]')?.querySelector('button, [tabindex]') as HTMLElement | null)?.focus());
	await page.evaluate((n) => n, name);
}
async function tabUntil(page: Page, label: RegExp, max = 14) {
	for (let i = 0; i < max; i++) {
		await page.keyboard.press('Tab');
		if (label.test(await active(page))) return true;
	}
	return false;
}
const LAYOUT = { width: 393, height: 852 };

// F1 + F7 + F5: keyboard walk. Focus after Cancel (waiting), after the row's confirm opens and is cancelled, after a row leaves, after the last row leaves.
test('F1 F5 F7 keyboard walk', async ({ page }) => {
	test.setTimeout(120_000);
	const result: Record<string, unknown> = {};
	// 1. Waiting for permission: Tab order, Cancel, then focus.
	await page.setViewportSize(LAYOUT);
	await openState(page, 'waiting');
	await group(page).scrollIntoViewIfNeeded();
	await focusBefore(page, 'waiting');
	const order: string[] = [];
	for (let i = 0; i < 6; i++) { await page.keyboard.press('Tab'); const a = await active(page); order.push(a); if (order.length === 1) await frame(page, 'F7-waiting-tab-on-switch-393', group(page)); if (order.length === 2) await frame(page, 'F7-waiting-tab-on-cancel-393', group(page)); if (!/inGroup=true/.test(a) && order.length > 1 && /inGroup=true/.test(order[order.length - 2])) break; }
	result.waitingTabOrder = order;
	await group(page).getByRole('button', { name: 'Cancel' }).focus();
	await page.keyboard.press('Enter');
	await page.waitForTimeout(400);
	result.afterCancelWaiting = await active(page);
	await frame(page, 'F1-after-cancel-waiting-393', group(page));

	// 2. Other devices: confirm opens, Cancel, confirm, row leaves.
	const rows = [
		['Turn off urgent notifications on iPhone', 'iPhone'],
		['Remove Chrome on Mac', 'Chrome on Mac'],
	];
	await openState(page, 'on-others');
	await group(page).scrollIntoViewIfNeeded();
	const log: Record<string, string> = {};
	const turnOff = group(page).getByRole('button', { name: /^Turn off urgent notifications on iPhone/ });
	await turnOff.focus();
	await page.keyboard.press('Enter');
	await page.waitForTimeout(450);
	log['1 Turn off pressed: confirm opens'] = await active(page);
	await frame(page, 'F5-confirm-turn-off-focus-393', group(page));
	await page.keyboard.press('Enter'); // Cancel
	await page.waitForTimeout(450);
	log['2 Cancel pressed'] = await active(page);
	await frame(page, 'F1-after-cancel-confirm-393', group(page));
	await page.keyboard.press('Enter'); // Turn off again: confirm opens
	await page.waitForTimeout(450);
	log['3 Turn off pressed again'] = await active(page);
	await page.keyboard.press('Shift+Tab'); // the confirm button
	log['4 Shift+Tab (the confirm button)'] = await active(page);
	await page.keyboard.press('Enter'); // confirm: row leaves
	await page.waitForTimeout(700);
	log['5 Confirmed: iPhone row gone, focus is'] = await active(page);
	await frame(page, 'F1-after-row-leaves-393', group(page));
	await page.keyboard.press('Enter'); // Remove on the Mac: confirm opens
	await page.waitForTimeout(450);
	await page.keyboard.press('Shift+Tab');
	await page.keyboard.press('Enter'); // confirm: last row leaves
	await page.waitForTimeout(700);
	log['6 Mac row removed (last row), focus is'] = await active(page);
	await frame(page, 'F1-after-last-row-leaves-393', group(page));
	result.otherDevices = log;
	void rows;
	save('F1-F5-F7-focus.json', result);
});

// F2: two stopped devices of one kind carry their dates; one-line attempt measured with canvas text widths.
test('F2 stopped line fit', async ({ page }) => {
	const out: Record<string, unknown> = {};
	for (const width of [380, 393, 1440]) {
		await page.setViewportSize({ width, height: SIZES[width as keyof typeof SIZES] });
		await openState(page, 'same-kind-stopped');
		await group(page).evaluate((el) => el.scrollIntoView({ block: 'center' }));
		await frame(page, `F2-same-kind-stopped-${width}`);
		out[width] = await group(page).evaluate((section) => {
			const row = section.querySelectorAll('.settings-row')[1];
			const sub = row.querySelector('.sub') as HTMLElement;
			const face = row.querySelector('.copy') as HTMLElement;
			const cs = getComputedStyle(sub);
			const ctx = document.createElement('canvas').getContext('2d')!;
			ctx.font = `${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
			const w = (t: string) => Math.round(ctx.measureText(t).width);
			const candidates = [
				'Stopped on its own · Added Nov 24',
				'Stopped on its own · Added Nov 24 · Turn it on there',
				'Stopped on its own · Added Nov 24 · Turn it on again from that device.',
				'Stopped · Added Nov 24 · Turn it on from that device',
			];
			return {
				copyColumnWidth: face ? Math.round(face.getBoundingClientRect().width) : null,
				fontSize: cs.fontSize,
				candidateWidths: Object.fromEntries(candidates.map((c) => [c, w(c)])),
				rows: [...section.querySelectorAll('.settings-row')].map((r) => ({ name: r.querySelector('.name')?.textContent?.trim(), sub: r.querySelector('.sub')?.textContent?.trim(), note: r.querySelector('[data-row-note]')?.textContent?.trim() ?? null, height: Math.round(r.getBoundingClientRect().height) })),
			};
		});
	}
	save('F2-stopped-fit.json', out);
});

// F3 F4: one treatment for the group's text actions, beside Log out.
test('F3 F4 action treatment', async ({ page }) => {
	const out: Record<string, unknown> = {};
	for (const width of [393, 1440]) {
		await page.setViewportSize({ width, height: SIZES[width as keyof typeof SIZES] });
		await openState(page, 'waiting');
		const measure = (name: string | RegExp, scope: Locator) => scope.getByRole('button', { name }).first().evaluate((el) => {
			const r = el.getBoundingClientRect();
			const cs = getComputedStyle(el);
			const after = getComputedStyle(el, '::after');
			const border = parseFloat(cs.borderTopWidth);
			const insetV = after.content !== 'none' ? -(parseFloat(after.top) + parseFloat(after.bottom)) : 0;
			const insetH = after.content !== 'none' ? -(parseFloat(after.left) + parseFloat(after.right)) : 0;
			return { text: el.textContent?.trim(), font: `${cs.fontSize}/${cs.fontWeight} ${cs.fontFamily.split(',')[0]}`, color: cs.color, boxW: Math.round(r.width * 10) / 10, boxH: Math.round(r.height * 10) / 10, hitW: Math.round((r.width - 2 * border + insetH) * 10) / 10, hitH: Math.round((r.height - 2 * border + insetV) * 10) / 10, classes: el.className };
		});
		const entry: Record<string, unknown> = {};
		entry.cancelWaiting = await measure('Cancel', group(page));
		const logOut = page.getByRole('button', { name: 'Log out' });
		await logOut.scrollIntoViewIfNeeded();
		entry.logOut = await measure('Log out', page.locator('body'));
		entry.restart = await measure('Restart', page.locator('body'));
		await openState(page, 'on-others');
		entry.turnOff = await measure(/^Turn off urgent notifications on iPhone/, group(page));
		entry.remove = await measure(/^Remove Chrome on Mac/, group(page));
		out[width] = entry;
	}
	save('F3-F4-actions.json', out);
});

// F6: the locked switch is drawn as disabled.
test('F6 locked switch', async ({ page }) => {
	test.setTimeout(120_000);
	const out: Record<string, unknown> = {};
	const read = () => page.getByRole('button', { name: 'Urgent notifications on this device' }).evaluate((el) => {
		const cs = getComputedStyle(el);
		return { opacity: cs.opacity, cursor: cs.cursor, ariaDisabled: el.getAttribute('aria-disabled'), ariaPressed: el.getAttribute('aria-pressed'), ariaChecked: el.getAttribute('aria-checked') };
	});
	for (const state of ['off', 'on', 'waiting', 'pending-this']) {
		await page.setViewportSize(LAYOUT);
		if (state === 'on') { await openState(page, 'on-others'); } else await openState(page, state);
		out[state] = await read();
		await group(page).evaluate((el) => el.scrollIntoView({ block: 'center' }));
		if (state === 'waiting' || state === 'pending-this') await frame(page, `F6-${state}-393`, group(page));
	}
	save('F6-locked-switch.json', out);
});

// F8: how far the rows below move when a state line appears, frame by frame, for three treatments.
test('F8 motion', async ({ page }) => {
	const out: Record<string, unknown> = {};
	const treatments: Record<string, string> = {
		'height transition (design)': '',
		'no transition (round 3 behaviour: appears at once)': `${GROUP} * { transition: none !important; }`,
		'reserved space (line space always held)': `${GROUP} .reveal[data-r] { grid-template-rows: 1fr !important; margin-top: 0 !important; } ${GROUP} .reveal[data-r] .inner { min-height: 1.6em; }`,
	};
	for (const [name, css] of Object.entries(treatments)) {
		await page.setViewportSize(LAYOUT);
		await openState(page, 'on-others');
		if (css) { await page.addStyleTag({ content: css }); await page.evaluate(() => document.querySelector('section[aria-label="Urgent notifications"] .reveal')?.setAttribute('data-r', '1')); }
		await group(page).evaluate((el) => el.scrollIntoView({ block: 'center' }));
		await page.waitForTimeout(400);
		const sw = page.getByRole('button', { name: 'Urgent notifications on this device' });
		// The row below This device is the iPhone row. Hold the server so "Turning off…" stays on screen.
		await page.route('**/api/push/devices/**', () => undefined);
		const probe = await page.evaluate(() => {
			const below = document.querySelectorAll('section[aria-label="Urgent notifications"] .settings-row')[1] as HTMLElement;
			const group = document.querySelector('section[aria-label="Urgent notifications"]') as HTMLElement;
			const samples: Array<{ t: number; rowBelowTop: number; groupHeight: number }> = [];
			const t0 = performance.now();
			const startTop = below.getBoundingClientRect().top;
			(window as unknown as { __probe: unknown }).__probe = { samples, startTop };
			const tick = () => { samples.push({ t: Math.round(performance.now() - t0), rowBelowTop: Math.round((below.getBoundingClientRect().top) * 10) / 10, groupHeight: Math.round(group.getBoundingClientRect().height * 10) / 10 }); if (performance.now() - t0 < 900) requestAnimationFrame(tick); };
			return { startTop };
		});
		void probe;
		await page.evaluate(() => {
			const below = document.querySelectorAll('section[aria-label="Urgent notifications"] .settings-row')[1] as HTMLElement;
			const p = (window as unknown as { __probe: { samples: Array<{ t: number; rowBelowTop: number }>; startTop: number } }).__probe;
			const t0 = performance.now();
			const tick = () => { p.samples.push({ t: Math.round(performance.now() - t0), rowBelowTop: Math.round(below.getBoundingClientRect().top * 10) / 10 }); if (performance.now() - t0 < 900) requestAnimationFrame(tick); };
			requestAnimationFrame(tick);
		});
		await sw.click();
		await page.waitForTimeout(1000);
		const data = await page.evaluate(() => (window as unknown as { __probe: { samples: Array<{ t: number; rowBelowTop: number }>; startTop: number } }).__probe);
		const tops = data.samples.map((s) => s.rowBelowTop);
		const steps = tops.slice(1).map((v, i) => Math.round((v - tops[i]) * 10) / 10);
		out[name] = {
			startTop: data.startTop,
			endTop: tops[tops.length - 1],
			totalMoved: Math.round((tops[tops.length - 1] - data.startTop) * 10) / 10,
			largestSingleFrameStep: Math.max(...steps.map(Math.abs)),
			framesThatMoved: steps.filter((s) => Math.abs(s) > 0.05).length,
			samples: data.samples.filter((_, i, a) => i === 0 || Math.abs(a[i].rowBelowTop - a[i - 1].rowBelowTop) > 0.05).slice(0, 40),
		};
		if (name.startsWith('height')) await frame(page, 'F8-turning-off-line-393', group(page));
		// Also: how much space the reserved treatment costs in the common On state (no line).
		out[`${name} / group height with no line`] = await group(page).evaluate((el) => Math.round(el.getBoundingClientRect().height));
	}
	save('F8-motion.json', out);
});

// F9: the gap after the ellipsis is one space, at every width.
test('F9 shortened names', async ({ page }) => {
	const out: Record<string, unknown> = {};
	await page.setViewportSize({ width: 380, height: 1200 });
	await openState(page, 'many-long-names');
	for (const width of [380, 394, 431, 461, 481, 521, 600, 1440]) {
		await page.setViewportSize({ width, height: 1200 });
		await page.waitForTimeout(350);
		out[width] = await group(page).evaluate((section) => {
			const ctx = document.createElement('canvas').getContext('2d')!;
			return [...section.querySelectorAll('.mid')].map((mid) => {
				const cs = getComputedStyle(mid);
				ctx.font = `${cs.fontStyle} ${cs.fontWeight} ${cs.fontSize} ${cs.fontFamily}`;
				const space = ctx.measureText(' ').width;
				const node = mid.firstChild as Text;
				const text = node.textContent ?? '';
				const at = text.indexOf('\u2026');
				const rect = (i: number) => { const r = document.createRange(); r.setStart(node, i); r.setEnd(node, i + 1); return r.getBoundingClientRect(); };
				let next = at + 1; while (text[next] === ' ') next++;
				// The gap the eye sees after the ellipsis: from its right edge to the first letter of the end.
				const gap = at >= 0 && next < text.length ? Math.round((rect(next).left - rect(at).right) * 10) / 10 : null;
				return { title: mid.getAttribute('title'), shown: text, shortened: at >= 0, oneLine: mid.getClientRects().length === 1 && mid.scrollWidth <= mid.clientWidth + 1, spacesAfterEllipsis: at >= 0 ? next - at - 1 : null, gapAfterEllipsisPx: gap, oneSpaceWidthPx: Math.round(space * 10) / 10 };
			});
		});
		if ([380, 394, 431, 461, 481, 521].includes(width)) await frame(page, `F9-many-long-names-${width}`, group(page));
	}
	save('F9-names.json', out);
});

// F12: /?activity=<id> opens that item; the address is cleaned; an id that is gone opens Activity's top item.
const title = 'Approve purchase order inv-2291, or let it lapse at 17:00?';
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
	const items = [item('mail-114', 'Reply to the landlord about the lease renewal?', 1), item('inv-2291', title, 2)];
	await page.route(/\/api\/attention\/queue\?(.*&)?view=activity/, (route) => route.fulfill({
		status: 200, contentType: 'application/json', body: JSON.stringify({ count: items.length, items, ranker: { thread_id: thread, label: 'CORSIA' } }),
	}));
}
for (const [size, height] of [[393, 852], [1440, 1000]] as const) {
	test(`F12 tap lands ${size}`, async ({ page, request }) => {
		test.setTimeout(180_000);
		const out: Record<string, unknown> = {};
		await activityFixture(page, request);
		await install(page, {});
		await loginByUrl(page, { waitForComposer: false });
		await page.setViewportSize({ width: size, height });
		// The fresh instance is still in its first-run landing, which opens the CORSIA thread on every load; a person tapping a push is past it.
		await page.route('**/api/corsia-agent*', async (route) => {
			const response = await route.fetch();
			const body = await response.json();
			await route.fulfill({ response, json: { ...body, first_run_pending: false } });
		});
		const opened = async (shot: string, expected: string, extra: string) => {
			await expect(page.getByText(expected, { exact: true }).first()).toBeVisible({ timeout: 45000 });
			await page.waitForTimeout(600);
			await page.mouse.move(0, 0);
			await page.screenshot({ path: join(OUT, 'frames', `${shot}.png`), animations: 'disabled' });
			return { url: page.url(), note: extra };
		};
		// The item named by the address; it is the second in the list, so a landing on the top item would show the lease question instead.
		await page.goto('/?activity=inv-2291');
		out.known = await opened(`F12-tap-lands-${size}`, 'Let it lapse', 'inv-2291 is second in the list; its decision is open');
		const knownUrl = new URL(page.url());
		out.knownAddressCleaned = !knownUrl.searchParams.has('activity');
		await page.reload();
		await page.waitForTimeout(4000);
		await page.screenshot({ path: join(OUT, 'frames', `F12-after-reload-${size}.png`), animations: 'disabled' });
		out.afterReload = { url: page.url(), note: 'read the frame: the fixture shows the same two answers for every item, so text cannot tell which item is open', topItemInList: await page.getByText('Reply to the landlord', { exact: false }).first().isVisible().catch(() => false) };
		await page.goto('/?activity=answered-long-ago');
		out.gone = await opened(`F12-id-gone-${size}`, 'Reply to the landlord about the lease renewal?', 'unknown id: Activity opens on its top item');
		out.goneAddressCleaned = !new URL(page.url()).searchParams.has('activity');
		save(`F12-${size}.json`, out);
	});
}

// F13d: the hint at most two lines at 380 and 393 for every company that can be named, and with none.
const HOSTS: Record<string, string> = {
	Apple: 'https://web.push.apple.com/FAKE',
	Google: 'https://fcm.googleapis.com/fcm/send/FAKE',
	Mozilla: 'https://updates.push.services.mozilla.com/FAKE',
	Microsoft: 'https://wns2.notify.windows.com/FAKE',
};
test('F13 hint lines per company', async ({ page }) => {
	test.setTimeout(240_000);
	const out: Record<string, unknown> = {};
	for (const [company, endpoint] of Object.entries(HOSTS)) {
		setups['hint-co'] = { subscribed: true, endpoint, devices: [{ id: 'this', name: 'This browser', state: 'on', endpoint_id: 'x' }] } as never;
		await page.setViewportSize({ width: 393, height: 852 });
		await openState(page, 'hint-co');
		for (const width of [380, 393, 1440]) {
			await page.setViewportSize({ width, height: 852 });
			await page.waitForTimeout(300);
			const hint = group(page).locator('.hint');
			const measured = await hint.evaluate((el) => {
				const rects: DOMRect[] = [];
				const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
				for (let n = walker.nextNode(); n; n = walker.nextNode()) { const r = document.createRange(); r.selectNodeContents(n); rects.push(...r.getClientRects()); }
				return { text: el.textContent!.trim(), lines: new Set(rects.map((r) => Math.round(r.top))).size };
			});
			out[`${company}-${width}`] = measured;
			if (width === 380 && company === 'Microsoft') await frame(page, 'F13-hint-Microsoft-380', group(page));
		}
	}
	save('F13-hint-companies.json', out);
	for (const [k, v] of Object.entries(out)) if (!k.endsWith('-1440')) expect((v as { lines: number }).lines, k).toBeLessThanOrEqual(2);
});

// Sweep: every state × width × theme.
const WIDTHS = [380, 393, 400, 420, 430, 440, 460, 480, 520, 600, 768, 1024, 1440, 1920];
// One test per state: each gets a fresh page, so a state's fakes never stack on the previous state's.
for (const state of Object.keys(setups)) test(`sweep ${state}`, async ({ page }) => {
	test.setTimeout(420_000);
	const all: Record<string, unknown> = {};
	{
		await page.setViewportSize({ width: 393, height: 1000 });
		await openState(page, state);
		for (const width of WIDTHS) {
			for (const theme of width === 380 || width === 393 || width === 1440 ? (['light', 'dark'] as const) : (['light'] as const)) {
				await page.setViewportSize({ width, height: 1000 });
				await page.emulateMedia({ colorScheme: theme });
				await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
				await page.waitForTimeout(350);
				await group(page).evaluate((el) => el.scrollIntoView({ block: 'center' }));
				all[`${state}-${width}-${theme}`] = await group(page).evaluate((section, w) => {
					const lines = (el: Element | null) => {
						if (!el) return 0;
						const rects: DOMRect[] = [];
						const walker = document.createTreeWalker(el, NodeFilter.SHOW_TEXT);
						for (let n = walker.nextNode(); n; n = walker.nextNode()) { if (!n.textContent?.trim()) continue; const r = document.createRange(); r.selectNodeContents(n); rects.push(...r.getClientRects()); }
						rects.sort((a, b) => a.top - b.top);
						let c = 0; let bottom = -Infinity;
						for (const r of rects) { if (r.top >= bottom - 1) { c++; bottom = r.bottom; } else bottom = Math.max(bottom, r.bottom); }
						return c;
					};
					const canvas = document.createElement('canvas'); canvas.width = canvas.height = 1;
					const ctx = canvas.getContext('2d', { willReadFrequently: true })!;
					const rgba = (c: string) => { ctx.clearRect(0, 0, 1, 1); ctx.fillStyle = '#000'; ctx.fillStyle = c; ctx.fillRect(0, 0, 1, 1); const d = ctx.getImageData(0, 0, 1, 1).data; return [d[0], d[1], d[2], d[3] / 255]; };
					const lum = ([r, g, b]: number[]) => { const f = (v: number) => { v /= 255; return v <= 0.03928 ? v / 12.92 : ((v + 0.055) / 1.055) ** 2.4; }; return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b); };
					const bgOf = (el: HTMLElement) => {
						const layers: number[][] = [];
						for (let e: HTMLElement | null = el; e; e = e.parentElement) { const c = rgba(getComputedStyle(e).backgroundColor); if (c[3] > 0) layers.push(c); if (c[3] >= 1) break; }
						let base = rgba(getComputedStyle(document.body).backgroundColor).slice(0, 3);
						for (const c of layers.reverse()) base = base.map((v, i) => c[i] * c[3] + v * (1 - c[3]));
						return base;
					};
					const sec = section as HTMLElement;
					const secRect = sec.getBoundingClientRect();
					const contrast: Array<{ text: string; ratio: number }> = [];
					const walker = document.createTreeWalker(sec, NodeFilter.SHOW_TEXT);
					for (let n = walker.nextNode(); n; n = walker.nextNode()) {
						const text = n.textContent?.trim(); if (!text) continue;
						const el = n.parentElement as HTMLElement;
						if (el.closest('.sr-only, [inert]:not(.inner)')) { /* still measured when visible */ }
						const cs = getComputedStyle(el);
						if (cs.visibility === 'hidden') continue;
						// A line that is collapsed has no height; skip text that is not on screen.
						const rr = el.getBoundingClientRect(); if (rr.height === 0 || rr.width === 0) continue;
						const fg = rgba(cs.color); const bg = bgOf(el);
						const fgc = fg.slice(0, 3).map((v, i) => v * fg[3] + bg[i] * (1 - fg[3]));
						const [a, b] = [lum(fgc), lum(bg)];
						contrast.push({ text: text.slice(0, 40), ratio: Math.round(((Math.max(a, b) + 0.05) / (Math.min(a, b) + 0.05)) * 100) / 100 });
					}
					const clipped: string[] = [];
					for (const el of sec.querySelectorAll<HTMLElement>('.sub, .words, .hint, .confirm-text, .confirm-detail, .name')) {
						if (el.closest('.reveal:not(.open)')) continue;
						if (el.classList.contains('name')) { const head = el.querySelector('.head') as HTMLElement | null; if (head && head.scrollWidth > head.clientWidth) continue; }
						if (el.scrollWidth > el.clientWidth + 1) clipped.push(el.textContent!.trim().slice(0, 40));
						const r = el.getBoundingClientRect(); if (r.right > secRect.right + 1 || r.left < secRect.left - 1) clipped.push(`outside: ${el.textContent!.trim().slice(0, 40)}`);
					}
					const targets = [...sec.querySelectorAll<HTMLElement>('button, [role="switch"]')].filter((el) => !el.closest('[inert]')).map((el) => {
						const r = el.getBoundingClientRect(); const after = getComputedStyle(el, '::after'); const border = parseFloat(getComputedStyle(el).borderTopWidth) || 0;
						const v = after.content !== 'none' && after.position === 'absolute' ? -(parseFloat(after.top) + parseFloat(after.bottom)) : 0;
						const hit = el.querySelector('.hit') as HTMLElement | null;
						const hitH = hit ? hit.getBoundingClientRect().height : r.height - 2 * border + v;
						const hitW = hit ? hit.getBoundingClientRect().width : r.width;
						return { name: (el.getAttribute('aria-label') ?? el.textContent ?? '').trim().slice(0, 50), h: Math.round(hitH * 10) / 10, w: Math.round(hitW * 10) / 10 };
					});
					const hint = sec.querySelector('.hint');
					const stateLines = [...sec.querySelectorAll<HTMLElement>('.sub, .reveal.open .words')].map((el) => ({ text: el.textContent!.trim(), lines: lines(el) }));
					const names = [...sec.querySelectorAll('.name')].map((el) => ({ text: el.textContent!.trim().slice(0, 40), lines: lines(el) }));
					return { width: w, hintLines: lines(hint), stateLines, names, rowHeights: [...sec.querySelectorAll('.settings-row')].map((r) => Math.round(r.getBoundingClientRect().height)), clipped, targets, minContrast: contrast.length ? Math.min(...contrast.map((c) => c.ratio)) : null, lowContrast: contrast.filter((c) => c.ratio < 4.5), docOverflow: document.documentElement.scrollWidth > document.documentElement.clientWidth };
				}, width);
			}
		}
	}
	save(`sweep-${state}.json`, all);
});
