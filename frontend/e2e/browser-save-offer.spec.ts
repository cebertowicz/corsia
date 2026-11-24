import fs from 'node:fs';
import path from 'node:path';
import { test } from './support/test';
import { expect, type Page, type Route, type WebSocketRoute } from '@playwright/test';
import { createSession, loginByUrl, replacePreviousFixtureThread, sidebarThread } from './support/corsia';
import { isolatedDb, setDebugVersionOverlay } from './support/heldComments';
import { CONFIRMATION_HOLD_MS } from '../src/lib/confirmationHold';

/** Set to a folder to save a screenshot of each state; unset, the spec only asserts. */
const OUT = process.env.OFFER_RENDER_DIR;
const SIZES = [{ name: '1440', width: 1440, height: 1000 }, { name: '768', width: 768, height: 1000 }, { name: '393', width: 393, height: 852 }];

type Offer = { site: string; domain: string; username: string; update: boolean };
const EXAMPLE: Offer = { site: 'https://example.com', domain: 'example.com', username: 'sam@example.com', update: false };
// A domain is never cut: the question wraps around it, over two lines or over more.
const TWO_LINE_DOMAIN = 'login.accounts.my-example-company.example.com';
const LONG_DOMAIN = 'sso.regional-operations.internal.my-example-company-holdings-international.example.com';

/** The size of the page the agent's viewport shows, set when the pane opens. */
const view = { width: 900, height: 600 };
/** What the server answers; the spec changes these between steps. */
const server = { offer: null as Offer | null, save: 200, saveBody: {} as unknown, saves: 0, notNows: 0, gets: 0 };

const SERVER_TEXT = 'Saving isn’t available for this sign-in. Unless the login is already saved, it’s offered again the next time you sign in.';
// The longest refusal the server sends: the sign-in's browser closed before Save.
const CLOSED_TEXT = 'The shared browser this sign-in came from has closed, so it can’t be saved. It’s offered again the next time you sign in.';
const PAGE_TITLE = 'Sign in';
const EXAMPLE_PAGE = { heading: PAGE_TITLE, footer: 'Create an account' };
// A company's own sign-in page, for the long site names: the tab, the address and the page itself belong to that site.
const COMPANY_PAGE = { heading: 'Sign in to Regional Operations', footer: 'Need access? Ask your administrator' };

/** A sign-in page drawn at the size the agent's viewport has, so it fills the pane edge to edge in the person's colours. */
async function pageImage(page: Page, width: number, height: number, scheme: 'light' | 'dark', copy: { heading: string; footer: string } = EXAMPLE_PAGE) {
	const palette = scheme === 'dark' ? { page: '#0d1117', card: '#161b22', line: '#30363d', text: '#e6edf3', quiet: '#8b949e', field: '#0d1117' } : { page: '#ffffff', card: '#f6f8fa', line: '#d0d7de', text: '#1f2328', quiet: '#59636e', field: '#ffffff' };
	const field = (label: string, value: string) => `<label style="display:block;margin:16px 0 4px;font-weight:600">${label}</label><div style="padding:6px 12px;height:32px;box-sizing:border-box;border:1px solid ${palette.line};border-radius:6px;background:${palette.field};line-height:18px">${value}</div>`;
	const shot = await page.context().newPage();
	await shot.setViewportSize({ width, height });
	await shot.setContent(`<body style="margin:0;height:100vh;background:${palette.page};color:${palette.text};font:14px -apple-system,'Segoe UI',Helvetica,Arial,sans-serif"><main style="width:min(308px,calc(100% - 32px));margin:0 auto;padding-top:${Math.round(height * 0.12)}px"><h1 style="margin:0 0 16px;font-size:24px;font-weight:300;text-align:center">${copy.heading}</h1><form style="padding:16px;border:1px solid ${palette.line};border-radius:6px;background:${palette.card}">${field('Username or email address', 'sam@example.com')}${field('Password', '••••••••••••')}<div style="margin-top:16px;padding:6px 12px;border-radius:6px;background:#4a5a78;color:#fff;font-weight:600;text-align:center">Sign in</div></form><p style="margin:16px 0 0;padding:16px;border:1px solid ${palette.line};border-radius:6px;text-align:center;color:${palette.quiet}">${copy.footer}</p></main></body>`);
	const data = (await shot.screenshot({ type: 'jpeg', quality: 85 })).toString('base64');
	await shot.close();
	return data;
}

/** Test residue off the screen, by the means the contact sheet uses: no build strip, a real model name, the person's own first name in the greeting. */
async function cleanData(page: Page, request: Parameters<typeof createSession>[0]) {
	await setDebugVersionOverlay(request, false);
	isolatedDb(['cursor = connection.execute("UPDATE models SET display_name=? WHERE id=?", ("Claude Sonnet 5.5", "mock-test-model"))', 'assert cursor.rowcount == 1']);
	await page.context().routeWebSocket('**/ws', (socket) => {
		const server = socket.connectToServer();
		server.onMessage((message) => {
			if (typeof message !== 'string') return socket.send(message);
			const payload = JSON.parse(message);
			if (payload.type === 'auth_ok' && payload.user) payload.user.name = 'Sam';
			socket.send(JSON.stringify(payload));
		});
	});
}

async function openPane(page: Page, request: Parameters<typeof createSession>[0], scheme: 'light' | 'dark') {
	Object.assign(server, { offer: null, save: 200, saveBody: {}, saves: 0, notNows: 0, gets: 0 });
	await page.addInitScript((value) => localStorage.setItem('corsia_theme', value), scheme);
	// The Saved line's hold and fade run on the page's clock, so a spec that wants to look at the fade steps that clock itself.
	await page.clock.install();
	if (OUT) await cleanData(page, request);
	let stream: WebSocketRoute | null = null;
	const resizes: unknown[] = [];
	await page.routeWebSocket((url) => /\/api\/browser\/[^/]+\/stream/.test(url.pathname), (ws) => {
		stream = ws;
		ws.onMessage((raw) => { try { const message = JSON.parse(String(raw)); if (message.type === 'appearance') resizes.push(message); } catch { /* the token frame */ } });
		ws.onMessage((raw) => { try { const message = JSON.parse(String(raw)); if (message.type === 'navigate') ws.send(JSON.stringify({ type: 'navigate_result', url: message.url, title: PAGE_TITLE, domain: 'example.com' })); } catch { /* the token frame */ } });
		ws.send(JSON.stringify({ type: 'browser_metadata', url: 'https://example.com/login', title: PAGE_TITLE, domain: 'example.com' }));
	});
	await page.route(/\/api\/browser\/[^/]+\/open/, (route: Route) => route.fulfill({ status: 200, json: {} }));
	await page.route(/\/api\/saved-logins\/offer(\?|$)/, (route: Route) => { server.gets++; return route.fulfill({ status: 200, json: { offer: server.offer } }); });
	await page.route(/\/api\/saved-logins\/offer\/save/, (route: Route) => { server.saves++; return route.fulfill({ status: server.save, json: server.save === 200 ? { saved: true } : server.saveBody }); });
	await page.route(/\/api\/saved-logins\/offer\/not-now/, (route: Route) => { server.notNows++; return route.fulfill({ status: 200, json: {} }); });
	const title = 'Sign in to the example site for me';
	await replacePreviousFixtureThread(request, await createSession(request, title));
	await loginByUrl(page);
	// The rail sits behind the main surface, which slides right to open it; wide windows start with it open, so toggle only a closed one.
	const railOpen = async () => ((await page.locator('main.main').boundingBox())?.x ?? 0) > 40;
	await page.waitForTimeout(600);
	if (!(await railOpen())) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await expect.poll(railOpen).toBe(true);
	await sidebarThread(page, title).click();
	// The panel's open state outlives a test, and a closed panel can still hold its button in the page where a click cannot reach it:
	// click it, and when the click cannot land, toggle the panel once and click again.
	const openBrowser = page.getByRole('button', { name: 'Open browser', exact: true });
	if (!(await openBrowser.click({ timeout: 4000 }).then(() => true, () => false))) {
		await page.getByRole('button', { name: 'Toggle panel' }).click();
		await openBrowser.click();
	}
	const address = page.getByRole('textbox', { name: 'Browser address' });
	await address.fill('https://example.com/login');
	await address.press('Enter');
	const canvas = page.locator('canvas[aria-label="Shared browser"]');
	await expect(canvas).toBeVisible();
	await expect.poll(() => stream !== null).toBe(true);
	const send = (message: object) => stream!.send(JSON.stringify(message));
	// The page arrives at the size of the canvas before any dock, as the agent's viewport has.
	await page.waitForTimeout(300);
	const pane = (await canvas.boundingBox())!;
	Object.assign(view, { width: Math.round(pane.width), height: Math.round(pane.height) });
	send({ type: 'frame', metadata: { deviceWidth: view.width, deviceHeight: view.height }, data: await pageImage(page, view.width, view.height, scheme) });
	return { canvas, send, resizes };
}

/**
 * The person signs in themselves: a password page opens, they type, the page settles.
 * On a phone they tap the field, which opens the pane's own text input, and type into that; otherwise they type on the keyboard.
 */
async function signIn(page: Page, canvas: ReturnType<Page['locator']>, send: (message: object) => void, touch = false) {
	send({ type: 'browser_password_page', visible: true });
	await expect(page.locator('.password-notice')).toBeVisible();
	if (touch) {
		// One field covering the whole page, so a tap in the middle of the canvas, where the page is drawn, lands in it.
		send({ type: 'text_fields', fields: [{ x: 0, y: 0, width: view.width, height: view.height, kind: 'password' }] });
		const box = (await canvas.boundingBox())!;
		const field = page.locator('input[data-pane-field]');
		// The field list arrives over the stream, so a tap that beats it is repeated.
		await expect(async () => {
			// A finger lands on whatever is on top there: the canvas the first time, the pane's own input after that.
			await page.touchscreen.tap(box.x + box.width / 2, box.y + box.height / 2);
			await expect(field).toBeFocused({ timeout: 1000 });
		}).toPass();
		await field.pressSequentially('x');
	} else {
		await canvas.click({ position: { x: 40, y: 40 } });
		await page.keyboard.type('x');
	}
	send({ type: 'browser_password_page', visible: false });
	// The your-step notice fades out before anything is framed.
	await expect(page.locator('.password-notice')).toHaveCount(0);
}

/** The pane's page moves to another site: its tab title, address and drawn page all say so, as they would in life. */
async function showSite(page: Page, send: (message: object) => void, scheme: 'light' | 'dark', domain: string, title: string, copy: { heading: string; footer: string }) {
	send({ type: 'browser_metadata', url: `https://${domain}/login`, title, domain });
	send({ type: 'frame', metadata: { deviceWidth: view.width, deviceHeight: view.height }, data: await pageImage(page, view.width, view.height, scheme, copy) });
	await page.waitForTimeout(300);
}

const dock = (page: Page) => page.locator('.save-offer');

async function shot(page: Page, size: string, scheme: string, state: string) {
	if (!OUT) return;
	fs.mkdirSync(OUT, { recursive: true });
	await page.mouse.move(2, 2);
	// The mock model's context meter reads a negative percentage; the frame leaves it out.
	await page.addStyleTag({ content: '[data-sheet-trigger][aria-label^="Context for"] { visibility: hidden; }' });
	// A frame shows the dock at rest: its arrival and its error line are animated.
	await page.waitForTimeout(350);
	await page.screenshot({ path: path.join(OUT, `${state}-${size}-${scheme}.png`) });
}

test.describe('the pane save-login offer', () => {
	test.describe.configure({ timeout: 120_000 });

	for (const size of SIZES) test.describe(`at ${size.name}`, () => {
		// The phone is a touch device: its person taps the field and types into the pane's own input.
		const touch = size.width === 393;
		test.use({ hasTouch: touch });

		for (const scheme of ['light', 'dark'] as const) {
			test(`walks every state at ${size.name}, ${scheme}`, async ({ page, request }) => {
				await page.setViewportSize({ width: size.width, height: size.height });
				const { canvas, send, resizes } = await openPane(page, request, scheme);
				await page.waitForTimeout(800);
				const settled = resizes.length;

				server.offer = EXAMPLE;
				await signIn(page, canvas, send, touch);
				await expect(dock(page)).toBeVisible();
				await expect(dock(page)).toContainText('Save the example.com login for agents?');
				await expect(dock(page)).toContainText('sam@example.com');
				await expect(dock(page).locator('input, textarea')).toHaveCount(0);
				// It lies over the page's bottom edge: flush with it, over the canvas, which keeps its size.
				await page.waitForTimeout(400);
				const [dockBox, canvasBox] = await Promise.all([dock(page).boundingBox(), canvas.boundingBox()]);
				expect(dockBox!.y + dockBox!.height).toBeCloseTo(canvasBox!.y + canvasBox!.height, 0);
				expect(dockBox!.y).toBeLessThan(canvasBox!.y + canvasBox!.height);
				expect(Math.abs(canvasBox!.width - view.width)).toBeLessThanOrEqual(1);
				expect(Math.abs(canvasBox!.height - view.height)).toBeLessThanOrEqual(1);
				// The surface of the document review bar's dock: secondary background, rounded top corners, and its hairline on the top and sides, none along the bottom.
				const surface = await dock(page).evaluate((node) => {
					const style = getComputedStyle(node);
					const probe = document.createElement('i');
					probe.style.cssText = 'background: var(--bg-secondary); border-radius: var(--radius-xl); border: 1px solid var(--border-subtle)';
					document.body.append(probe);
					const expected = { background: getComputedStyle(probe).backgroundColor, radius: getComputedStyle(probe).borderTopLeftRadius, line: getComputedStyle(probe).borderTopColor };
					probe.remove();
					return { background: style.backgroundColor, topLeft: style.borderTopLeftRadius, topRight: style.borderTopRightRadius, bottomLeft: style.borderBottomLeftRadius, widths: [style.borderTopWidth, style.borderLeftWidth, style.borderRightWidth, style.borderBottomWidth], lines: [style.borderTopColor, style.borderLeftColor, style.borderRightColor], expected };
				});
				expect(surface.background).toBe(surface.expected.background);
				expect([surface.topLeft, surface.topRight]).toEqual([surface.expected.radius, surface.expected.radius]);
				expect(surface.bottomLeft).toBe('0px');
				expect(surface.widths).toEqual(['1px', '1px', '1px', '0px']);
				expect(surface.lines).toEqual([surface.expected.line, surface.expected.line, surface.expected.line]);
				// Save and Not now are a pair on the right. Wide enough, the pair sits to the right of the question; narrower, it takes its own row below, still on the right.
				const [saveBox, notNowBox, copyBox] = await Promise.all([
					dock(page).getByRole('button', { name: 'Save' }).boundingBox(),
					dock(page).getByRole('button', { name: 'Not now' }).boundingBox(),
					dock(page).locator('.copy').boundingBox(),
				]);
				if (dockBox!.width >= 576) expect(saveBox!.x).toBeGreaterThanOrEqual(copyBox!.x + copyBox!.width - 1);
				else expect(saveBox!.y).toBeGreaterThanOrEqual(copyBox!.y + copyBox!.height - 1);
				expect(dockBox!.x + dockBox!.width - (notNowBox!.x + notNowBox!.width)).toBeLessThanOrEqual(24);
				// Each button is a touch target at 768 and under.
				if (size.width <= 768) for (const box of [saveBox!, notNowBox!]) expect(box.height).toBeGreaterThanOrEqual(43.5);
				// Save and Not now do different things: at least the wider button's width of room sits between them.
				expect(notNowBox!.x - (saveBox!.x + saveBox!.width)).toBeGreaterThanOrEqual(Math.max(saveBox!.width, notNowBox!.width));
				// The agent's viewport did not resize because the dock appeared, and the screen reader hears what the screen shows.
				expect(resizes.length).toBe(settled);
				await expect(page.locator('[aria-live="polite"]', { hasText: 'Save the example.com login for agents? sam@example.com' }).first()).toBeAttached();
				await shot(page, size.name, scheme, '1-ideal');

				// Only a 404 or a 409 shows the server's words; any other failure says the pane's own line, and keeps the offer, with Save and Not now still there.
				server.save = 403;
				server.saveBody = { detail: SERVER_TEXT };
				await dock(page).getByRole('button', { name: 'Save' }).click();
				await expect(dock(page)).toContainText('Couldn’t save the login. Try again.');
				await expect(dock(page)).not.toContainText(SERVER_TEXT);
				server.save = 500;
				server.saveBody = { detail: 'internal' };
				await dock(page).getByRole('button', { name: 'Save' }).click();
				await expect(dock(page)).toContainText('Couldn’t save the login. Try again.');
				await expect(dock(page)).not.toContainText(SERVER_TEXT);
				await expect(dock(page).getByRole('button', { name: 'Save' })).toBeVisible();
				await expect(dock(page).getByRole('button', { name: 'Not now' })).toBeVisible();
				await shot(page, size.name, scheme, '2-error');

				// A save that works says Saved, holds, fades, and closes. The offer is asked for again first, so the
				// swap is framed on the plain question rather than on an error line that is still collapsing.
				server.save = 200;
				await dock(page).getByRole('button', { name: 'Not now' }).click();
				await expect(dock(page)).toHaveCount(0);
				await signIn(page, canvas, send, touch);
				await expect(dock(page)).toBeVisible();
				await page.waitForTimeout(400);
				// A content swap has no size jump (motion.md): the dock keeps the height it had while it asked.
				const askingHeight = (await dock(page).boundingBox())!.height;
				// The page's clock is stopped here so the fade can be caught part-way.
				await page.clock.pauseAt((await page.evaluate(() => Date.now())) + 1000);
				await dock(page).getByRole('button', { name: 'Save' }).click();
				await expect(dock(page)).toContainText('Saved');
				await expect(page.locator('[aria-live="polite"]', { hasText: 'Saved' }).first()).toBeAttached();
				const savedHeight = (await dock(page).boundingBox())!.height;
				expect(Math.abs(savedHeight - askingHeight)).toBeLessThanOrEqual(1);
				// The measured pair goes in the report, so the design review can read the swap's size at each width.
				test.info().annotations.push({ type: 'dock height', description: `${size.name} ${scheme}: asking ${askingHeight.toFixed(1)} px, Saved ${savedHeight.toFixed(1)} px` });
				// The line at full opacity, before the fade starts: the state the fading frames never showed.
				await shot(page, size.name, scheme, '3-saved-held');
				await page.clock.runFor(CONFIRMATION_HOLD_MS);
				await expect(dock(page)).toHaveClass(/fading/);
				// Hold the fade 100 ms in, so the frame shows it part-way.
				await page.evaluate(() => document.querySelector('.save-offer')!.getAnimations().filter((animation) => animation instanceof CSSTransition).forEach((animation) => { animation.pause(); animation.currentTime = 100; }));
				const opacity = Number(await dock(page).evaluate((node) => getComputedStyle(node).opacity));
				expect(opacity).toBeGreaterThan(0);
				expect(opacity).toBeLessThan(1);
				await shot(page, size.name, scheme, '3-saved-fading');
				// The close timer waits exactly as long as the fade the stylesheet runs: the motion token.
				const fadeMs = Number.parseFloat(await page.evaluate(() => getComputedStyle(document.documentElement).getPropertyValue('--dur-standard')));
				await page.evaluate(() => document.querySelector('.save-offer')!.getAnimations().filter((animation) => animation instanceof CSSTransition).forEach((animation) => animation.play()));
				await page.clock.runFor(fadeMs - 1);
				await expect(dock(page)).toHaveCount(1);
				await page.clock.runFor(1);
				await expect(dock(page)).toHaveCount(0);
				await page.clock.resume();

				// An update offer.
				server.offer = { ...EXAMPLE, update: true };
				await signIn(page, canvas, send, touch);
				await expect(dock(page)).toContainText('Update the saved example.com login with this password?');
				await expect(dock(page).getByRole('button', { name: 'Update' })).toBeVisible();
				await shot(page, size.name, scheme, '4-update');
				await dock(page).getByRole('button', { name: 'Not now' }).click();
				await expect(dock(page)).toHaveCount(0);
				expect(server.notNows).toBe(2);

				// A refusal takes the question's place at no smaller size, and a sentence that needs more room eases it in: the frame
				// after the swap is not yet at the full height (motion.md: a content swap has no size jump).
				const refuseMeasured = async (detail: string) => {
					server.offer = EXAMPLE;
					await signIn(page, canvas, send, touch);
					await expect(dock(page)).toContainText('Save the example.com login for agents?');
					await page.waitForTimeout(400);
					const asking = (await dock(page).boundingBox())!.height;
					await page.evaluate((text) => {
						const w = window as unknown as { swap: number[] };
						w.swap = [];
						const sample = () => {
							const node = document.querySelector('.save-offer');
							if (node?.querySelector('.question')?.textContent === text) w.swap.push(node.getBoundingClientRect().height);
							if (node && w.swap.length < 40) requestAnimationFrame(sample);
						};
						requestAnimationFrame(sample);
					}, detail);
					server.save = 409;
					server.saveBody = { detail };
					await dock(page).getByRole('button', { name: 'Save' }).click();
					await expect(dock(page).locator('.question')).toHaveText(detail);
					await page.waitForTimeout(400);
					const swap = await page.evaluate(() => (window as unknown as { swap: number[] }).swap);
					const first = swap[0];
					const refused = (await dock(page).boundingBox())!.height;
					// Frames caught part-way through the ease, strictly between the two heights.
					const between = swap.filter((height) => height > asking + 1 && height < refused - 1).length;
					expect(refused).toBeGreaterThanOrEqual(asking - 0.5);
					expect(first).toBeGreaterThanOrEqual(asking - 0.5);
					if (refused > asking + 1) {
						expect(first).toBeLessThan(refused - 1);
						expect(between).toBeGreaterThan(0);
					}
					test.info().annotations.push({ type: 'refusal height', description: `${size.name} ${scheme} ${detail === CLOSED_TEXT ? 'closed-browser' : 'no-offer'}: asking ${asking.toFixed(1)} px, first frame after the swap ${first.toFixed(1)} px, ${between} frames part-way, settled ${refused.toFixed(1)} px` });
				};

				// A 409 (or a 404) puts the server's sentence in the question's place, removes Save and Not now, leaves Close with focus, and never fades by itself.
				const savesBefore = server.saves;
				await refuseMeasured(SERVER_TEXT);
				await expect(page.locator('[aria-live="polite"]', { hasText: SERVER_TEXT }).first()).toBeAttached();
				await expect(dock(page).getByRole('button', { name: 'Save' })).toHaveCount(0);
				await expect(dock(page).getByRole('button', { name: 'Not now' })).toHaveCount(0);
				await expect(dock(page).getByRole('button', { name: 'Close' })).toBeFocused();
				// The sentence stands on its own: the username line is gone with the offer it named.
				await expect(dock(page).locator('.username')).toHaveCount(0);
				expect(server.saves).toBe(savesBefore + 1);
				if (size.name !== '768') await shot(page, size.name, scheme, '8-refused');
				await page.clock.pauseAt((await page.evaluate(() => Date.now())) + 1000);
				await page.clock.runFor(CONFIRMATION_HOLD_MS * 3);
				await expect(dock(page)).toBeVisible();
				await expect(dock(page)).not.toHaveClass(/fading/);
				await page.clock.resume();
				// Close dismisses as Not now does, and focus lands back on the page.
				await dock(page).getByRole('button', { name: 'Close' }).click();
				await expect(dock(page)).toHaveCount(0);
				expect(server.notNows).toBe(3);

				// The longest sentence, for a sign-in whose browser has since closed.
				await refuseMeasured(CLOSED_TEXT);
				await expect(dock(page).getByRole('button', { name: 'Close' })).toBeFocused();
				if (size.name !== '768') await shot(page, size.name, scheme, '8-refused-closed');
				await dock(page).getByRole('button', { name: 'Close' }).click();
				await expect(dock(page)).toHaveCount(0);
				server.save = 200;

				// A domain is never cut, however long: the question wraps around it and nothing runs past the dock.
				const wholeInside = () => dock(page).locator('.question').evaluate((node) => node.scrollWidth <= node.clientWidth && (node.closest('.save-offer') as HTMLElement).scrollWidth <= (node.closest('.save-offer') as HTMLElement).clientWidth);
				for (const domain of [TWO_LINE_DOMAIN, LONG_DOMAIN]) {
					await showSite(page, send, scheme, domain, 'Sign in · Regional Operations', COMPANY_PAGE);
					server.offer = { ...EXAMPLE, domain, site: `https://${domain}` };
					await signIn(page, canvas, send, touch);
					await expect(dock(page).locator('.question')).toContainText(domain);
					expect(await dock(page).locator('.question').textContent()).not.toContain('…');
					expect(await wholeInside()).toBe(true);
					if (domain === LONG_DOMAIN) await shot(page, size.name, scheme, '5-long-site');
					await dock(page).getByRole('button', { name: 'Not now' }).click();
					await expect(dock(page)).toHaveCount(0);
				}

				// A page that moves to another site by itself (a link, a form submit, an agent) closes the offer.
				await showSite(page, send, scheme, 'example.com', PAGE_TITLE, EXAMPLE_PAGE);
				server.offer = EXAMPLE;
				await signIn(page, canvas, send, touch);
				await expect(dock(page)).toBeVisible();
				send({ type: 'browser_metadata', url: 'https://example.org/next', title: 'Next', domain: 'example.org' });
				await expect(dock(page)).toHaveCount(0);

				// Nothing asked when the agent signed in alone.
				const asked = server.gets;
				send({ type: 'browser_password_page', visible: true });
				send({ type: 'browser_password_page', visible: false });
				await page.waitForTimeout(300);
				expect(server.gets).toBe(asked);
				await expect(dock(page)).toHaveCount(0);
			});
		}
	});

	test('works by keyboard alone: Tab order is Save then Not now, and focus lands on the page when it closes', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'This test sets its own desktop viewport; the phone walks run on both projects.');
		await page.setViewportSize({ width: 1440, height: 1000 });
		const { canvas, send } = await openPane(page, request, 'light');
		server.offer = EXAMPLE;
		await signIn(page, canvas, send);
		await expect(dock(page)).toBeVisible();
		// The offer did not take focus when it appeared.
		expect(await page.evaluate(() => !!document.activeElement?.closest('.save-offer'))).toBe(false);
		const inDock = () => page.evaluate(() => document.activeElement?.closest('.save-offer') ? document.activeElement.textContent?.trim() : null);
		// Keys typed into the pane go to the page, Tab included, so a keyboard user reaches the dock from the controls after it: backwards from the end.
		const enterDockBackwards = async () => {
			await page.evaluate(() => (document.activeElement as HTMLElement | null)?.blur());
			for (let i = 0; i < 40 && (await inDock()) === null; i++) await page.keyboard.press('Shift+Tab');
		};
		await enterDockBackwards();
		expect(await inDock()).toBe('Not now');
		await page.keyboard.press('Shift+Tab');
		expect(await inDock()).toBe('Save');
		// The focused button shows a ring in the accent, which a white-on-white default would not.
		const readRing = () => page.evaluate(() => {
			const style = getComputedStyle(document.activeElement as HTMLElement);
			const probe = document.createElement('i');
			probe.style.color = 'var(--accent)';
			document.body.append(probe);
			const accent = getComputedStyle(probe).color;
			probe.remove();
			return { style: style.outlineStyle, width: style.outlineWidth, color: style.outlineColor, accent };
		});
		// The button eases its properties for a moment (--dur-tight), so read the ring once it has settled.
		await expect.poll(async () => { const r = await readRing(); return `${r.style} ${r.width} ${r.color === r.accent}`; }).toBe('solid 2px true');
		await page.keyboard.press('Tab');
		expect(await inDock()).toBe('Not now');
		await page.keyboard.press('Enter');
		await expect(dock(page)).toHaveCount(0);
		expect(server.notNows).toBe(1);
		expect(await page.evaluate(() => document.activeElement !== document.body && !!document.activeElement?.closest('.browser-page'))).toBe(true);

		// Save by keyboard: focus is on the page before the Save button is removed.
		server.offer = EXAMPLE;
		await signIn(page, canvas, send);
		await expect(dock(page)).toBeVisible();
		await enterDockBackwards();
		await page.keyboard.press('Shift+Tab');
		expect(await inDock()).toBe('Save');
		await page.keyboard.press('Enter');
		await expect(dock(page)).toContainText('Saved');
		expect(await page.evaluate(() => document.activeElement !== document.body && !!document.activeElement?.closest('.browser-page'))).toBe(true);
		await expect(dock(page)).toHaveCount(0);
		expect(await page.evaluate(() => document.activeElement !== document.body)).toBe(true);
	});

	test('Save keeps its width and Not now stays put while the save is pending, and the dock says it is busy', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'This test sets its own desktop viewport; the phone walks run on both projects.');
		await page.setViewportSize({ width: 393, height: 852 });
		const { canvas, send } = await openPane(page, request, 'light');
		server.offer = EXAMPLE;
		await signIn(page, canvas, send);
		await expect(dock(page)).toBeVisible();
		await page.waitForTimeout(350);
		const save = dock(page).getByRole('button', { name: 'Save' });
		const notNow = dock(page).getByRole('button', { name: 'Not now' });
		const before = { save: (await save.boundingBox())!, notNow: (await notNow.boundingBox())! };
		let release: () => void = () => {};
		const held = new Promise<void>((resolve) => { release = resolve; });
		await page.route(/\/api\/saved-logins\/offer\/save/, async (route) => { await held; await route.fulfill({ status: 200, json: { saved: true } }); });
		await save.click();
		await expect(save).toHaveAttribute('aria-busy', 'true');
		await expect(dock(page)).toHaveAttribute('aria-busy', 'true');
		await page.waitForTimeout(400);
		const during = { save: (await save.boundingBox())!, notNow: (await notNow.boundingBox())! };
		expect(during.save.width).toBeCloseTo(before.save.width, 0);
		expect(during.notNow.x).toBeCloseTo(before.notNow.x, 0);
		release();
		await expect(dock(page)).toContainText('Saved');
	});

	test('a window that narrows while Saved shows grows the dock instead of clipping the line', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'This test sets its own viewport; the phone walks run on both projects.');
		await page.setViewportSize({ width: 393, height: 852 });
		const { canvas, send } = await openPane(page, request, 'light');
		server.offer = { ...EXAMPLE, domain: LONG_DOMAIN, site: `https://${LONG_DOMAIN}` };
		await signIn(page, canvas, send);
		await expect(dock(page)).toBeVisible();
		await page.waitForTimeout(400);
		// The page's clock stops before Save, so the Saved line cannot fade out while the window narrows.
		await page.clock.pauseAt((await page.evaluate(() => Date.now())) + 1000);
		await dock(page).getByRole('button', { name: 'Save' }).click();
		await expect(dock(page)).toContainText('Saved');
		// The held height is a floor, not a fixed box: a narrower window takes more room for the line and gets it.
		await page.setViewportSize({ width: 320, height: 852 });
		await page.waitForTimeout(400);
		const [dockBox, doneBox] = await Promise.all([dock(page).boundingBox(), dock(page).locator('.done').boundingBox()]);
		expect(doneBox!.y).toBeGreaterThanOrEqual(dockBox!.y - 1);
		expect(doneBox!.y + doneBox!.height).toBeLessThanOrEqual(dockBox!.y + dockBox!.height + 1);
		expect(doneBox!.x + doneBox!.width).toBeLessThanOrEqual(dockBox!.x + dockBox!.width + 1);
	});

	test('the dock slides and fades in, fades out, and the error line grows in, never popping', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'This test sets its own desktop viewport; the phone walks run on both projects.');
		await page.setViewportSize({ width: 768, height: 1000 });
		const { canvas, send } = await openPane(page, request, 'light');
		// One sample per animation frame: the dock's opacity and offset, and the error line's height.
		await page.evaluate(() => {
			const w = window as unknown as { samples: { opacity: number[]; offset: number[]; error: number[] } };
			w.samples = { opacity: [], offset: [], error: [] };
			const sample = () => {
				const node = document.querySelector('.save-offer');
				w.samples.opacity.push(node ? Number(getComputedStyle(node).opacity) : 0);
				w.samples.offset.push(node ? new DOMMatrixReadOnly(getComputedStyle(node).transform).m42 : 0);
				w.samples.error.push(document.querySelector('.save-offer .error')?.getBoundingClientRect().height ?? 0);
				requestAnimationFrame(sample);
			};
			sample();
		});
		const read = (key: 'opacity' | 'offset' | 'error') => page.evaluate((name) => (window as unknown as { samples: Record<string, number[]> }).samples[name], key);
		const mark = () => page.evaluate(() => (window as unknown as { samples: { opacity: number[] } }).samples.opacity.length);
		const between = (samples: number[], from: number, low: number, high: number) => samples.slice(from).some((value) => value > low && value < high);

		let from = await mark();
		server.offer = EXAMPLE;
		await signIn(page, canvas, send);
		await expect(dock(page)).toBeVisible();
		await page.waitForTimeout(400);
		// It arrives part-way faded and part-way along its slide, and ends at rest.
		expect(between(await read('opacity'), from, 0.05, 0.95)).toBe(true);
		expect((await read('offset')).slice(from).some((value) => value > 0.5)).toBe(true);
		expect((await read('offset')).at(-1)).toBe(0);

		from = await mark();
		server.save = 500;
		await dock(page).getByRole('button', { name: 'Save' }).click();
		await expect(dock(page)).toContainText('Couldn’t save the login. Try again.');
		await page.waitForTimeout(400);
		const errorHeights = (await read('error')).slice(from);
		expect(between(errorHeights, 0, 1, Math.max(...errorHeights) - 1)).toBe(true);

		from = await mark();
		await dock(page).getByRole('button', { name: 'Not now' }).click();
		await expect(dock(page)).toHaveCount(0);
		// It fades out where it stands: no slide on the way out.
		expect(between(await read('opacity'), from, 0.05, 0.95)).toBe(true);
		expect(Math.max(...(await read('offset')).slice(from))).toBeLessThan(0.5);
	});

	test('the arrival, caught part-way', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'This test sets its own desktop viewport; the phone walks run on both projects.');
		await page.setViewportSize({ width: 1440, height: 1000 });
		const { canvas, send } = await openPane(page, request, 'light');
		// The moment the dock is in the page, hold its arrival 40 ms in, so a frame can show it.
		await page.evaluate(() => {
			new MutationObserver(() => {
				const node = document.querySelector('.save-offer');
				if (!node || (node as HTMLElement).dataset.held) return;
				(node as HTMLElement).dataset.held = '1';
				node.getAnimations().forEach((animation) => { animation.pause(); animation.currentTime = 40; });
			}).observe(document.body, { childList: true, subtree: true });
		});
		server.offer = EXAMPLE;
		await signIn(page, canvas, send);
		await expect(dock(page)).toBeVisible();
		const midway = await dock(page).evaluate((node) => ({ opacity: Number(getComputedStyle(node).opacity), offset: new DOMMatrixReadOnly(getComputedStyle(node).transform).m42 }));
		expect(midway.opacity).toBeGreaterThan(0.05);
		expect(midway.opacity).toBeLessThan(0.95);
		expect(midway.offset).toBeGreaterThan(0.5);
		if (OUT) {
			fs.mkdirSync(OUT, { recursive: true });
			await page.mouse.move(2, 2);
			await page.addStyleTag({ content: '[data-sheet-trigger][aria-label^="Context for"] { visibility: hidden; }' });
			await page.screenshot({ path: path.join(OUT, '6-arriving-1440-light.png') });
		}
	});

	test('a touch screen gets 44 px targets at any width', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'This test sets its own desktop viewport; the phone walks run on both projects.');
		await page.setViewportSize({ width: 1440, height: 1000 });
		const { canvas, send } = await openPane(page, request, 'light');
		// The screen turns touch once the pane is open, as a tablet docked to a keyboard does.
		const touchScreen = await page.context().newCDPSession(page);
		await touchScreen.send('Emulation.setTouchEmulationEnabled', { enabled: true, maxTouchPoints: 5 });
		expect(await page.evaluate(() => matchMedia('(pointer: coarse)').matches)).toBe(true);
		server.offer = EXAMPLE;
		await signIn(page, canvas, send);
		for (const name of ['Save', 'Not now']) expect((await dock(page).getByRole('button', { name }).boundingBox())!.height).toBeGreaterThanOrEqual(43.5);
	});

	test('reduced motion crossfades the dock in and out and slides nothing', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'This test sets its own desktop viewport; the phone walks run on both projects.');
		await page.emulateMedia({ reducedMotion: 'reduce' });
		await page.setViewportSize({ width: 1440, height: 1000 });
		const { canvas, send } = await openPane(page, request, 'light');
		server.offer = EXAMPLE;
		await signIn(page, canvas, send);
		await expect(dock(page)).toBeVisible();
		// The arrival is a crossfade over the tight duration, with no slide.
		const arrival = await dock(page).evaluate((node) => { const style = getComputedStyle(node); return { name: style.animationName, duration: style.animationDuration, tight: getComputedStyle(document.documentElement).getPropertyValue('--dur-tight').trim() }; });
		expect(arrival.name).toContain('dock-fade');
		expect(Number.parseFloat(arrival.duration) * 1000).toBe(Number.parseFloat(arrival.tight));
		await dock(page).getByRole('button', { name: 'Save' }).click();
		await expect(dock(page)).toContainText('Saved');
		await expect(dock(page)).toHaveCount(0, { timeout: 3000 });
		expect(await dock(page).count()).toBe(0);
	});

	test.describe('on a phone', () => {
		test.use({ hasTouch: true });
		test('a phone with its keyboard up: the dock over a 516 px page', async ({ page, request }, testInfo) => {
			test.skip(testInfo.project.name !== 'chromium-desktop', 'This test sets its own viewport; the phone walks run on both projects.');
			await page.setViewportSize({ width: 393, height: 852 });
			const { canvas, send } = await openPane(page, request, 'light');
			server.offer = EXAMPLE;
			await signIn(page, canvas, send, true);
			await expect(dock(page)).toBeVisible();
			await page.setViewportSize({ width: 393, height: 516 });
			await page.waitForTimeout(500);
			const box = (await canvas.boundingBox())!;
			send({ type: 'frame', metadata: { deviceWidth: Math.round(box.width), deviceHeight: Math.round(box.height) }, data: await pageImage(page, Math.round(box.width), Math.round(box.height), 'light') });
			await page.waitForTimeout(300);
			const [dockBox, canvasBox] = await Promise.all([dock(page).boundingBox(), canvas.boundingBox()]);
			// The page keeps the whole canvas under the dock; the dock takes its bottom edge.
			expect(dockBox!.y + dockBox!.height).toBeCloseTo(canvasBox!.y + canvasBox!.height, 0);
			expect(canvasBox!.height).toBeGreaterThan(dockBox!.height * 2);
			await shot(page, '393x516', 'light', '7-keyboard');
		});
	});
});
