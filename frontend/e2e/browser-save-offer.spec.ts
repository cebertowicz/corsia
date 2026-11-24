import fs from 'node:fs';
import path from 'node:path';
import { test } from './support/test';
import { expect, type Page, type Route, type WebSocketRoute } from '@playwright/test';
import { createSession, loginByUrl, sidebarThread } from './support/corsia';

/** Set to a folder to save a screenshot of each state; unset, the spec only asserts. */
const OUT = process.env.OFFER_RENDER_DIR;
const SIZES = [{ name: '1440', width: 1440, height: 1000 }, { name: '768', width: 768, height: 1000 }, { name: '393', width: 393, height: 852 }];

type Offer = { site: string; domain: string; username: string; update: boolean };
const GITHUB: Offer = { site: 'https://github.com', domain: 'github.com', username: 'jordan@example.com', update: false };
const LONG_DOMAIN = `${'a-very-long-subdomain-name'.repeat(2)}.${'another-long-label'.repeat(2)}.example.org`;

/** What the server answers; the spec changes these between steps. */
const server = { offer: null as Offer | null, save: 200, saves: 0, notNows: 0, gets: 0 };

async function pageImage(page: Page) {
	const shot = await page.context().newPage();
	await shot.setViewportSize({ width: 900, height: 600 });
	await shot.setContent('<body style="margin:0;font:16px sans-serif;background:#f4f5f7"><h2 style="margin:0;padding:24px 32px;background:#fff">Your dashboard</h2><div style="margin:24px 32px;padding:24px;background:#fff;border:1px solid #ccd">Repositories</div><div style="margin:24px 32px;padding:24px;background:#fff;border:1px solid #ccd">Recent activity</div></body>');
	const data = (await shot.screenshot({ type: 'jpeg', quality: 80 })).toString('base64');
	await shot.close();
	return data;
}

async function openPane(page: Page, request: Parameters<typeof createSession>[0], scheme: 'light' | 'dark') {
	Object.assign(server, { offer: null, save: 200, saves: 0, notNows: 0, gets: 0 });
	await page.addInitScript((value) => localStorage.setItem('corsia_theme', value), scheme);
	// The dock closes 200 ms after its fade starts; a spec that wants to look at the fade stretches that one timer.
	await page.addInitScript(() => {
		const timeout = window.setTimeout.bind(window);
		window.setTimeout = ((handler: TimerHandler, ms?: number, ...rest: unknown[]) => timeout(handler, ms === 200 && (window as { slowClose?: boolean }).slowClose ? 8000 : ms, ...rest)) as typeof window.setTimeout;
	});
	const data = await pageImage(page);
	let stream: WebSocketRoute | null = null;
	const resizes: unknown[] = [];
	await page.routeWebSocket((url) => /\/api\/browser\/[^/]+\/stream/.test(url.pathname), (ws) => {
		stream = ws;
		ws.onMessage((raw) => { try { const message = JSON.parse(String(raw)); if (message.type === 'appearance') resizes.push(message); } catch { /* the token frame */ } });
		ws.send(JSON.stringify({ type: 'browser_metadata', url: 'https://github.com/dashboard', title: 'Dashboard', domain: 'github.com' }));
		ws.send(JSON.stringify({ type: 'frame', metadata: { deviceWidth: 900, deviceHeight: 600 }, data }));
	});
	await page.route(/\/api\/browser\/[^/]+\/open/, (route: Route) => route.fulfill({ status: 200, json: {} }));
	await page.route(/\/api\/saved-logins\/offer(\?|$)/, (route: Route) => { server.gets++; return route.fulfill({ status: 200, json: { offer: server.offer } }); });
	await page.route(/\/api\/saved-logins\/offer\/save/, (route: Route) => { server.saves++; return route.fulfill({ status: server.save, json: server.save === 200 ? { saved: true } : {} }); });
	await page.route(/\/api\/saved-logins\/offer\/not-now/, (route: Route) => { server.notNows++; return route.fulfill({ status: 200, json: {} }); });
	const title = `save-offer-${Date.now()}-${Math.random()}`;
	await createSession(request, title);
	await loginByUrl(page);
	await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await sidebarThread(page, title).click();
	// The panel's open state outlives a test, so toggle it only when an earlier test left it closed.
	const openBrowser = page.getByRole('button', { name: 'Open browser', exact: true });
	if (!(await openBrowser.isVisible())) await page.getByRole('button', { name: 'Toggle panel' }).click();
	await openBrowser.click();
	const address = page.getByRole('textbox', { name: 'Browser address' });
	await address.fill('https://github.com/login');
	await address.press('Enter');
	const canvas = page.locator('canvas[aria-label="Shared browser"]');
	await expect(canvas).toBeVisible();
	await expect.poll(() => stream !== null).toBe(true);
	const send = (message: object) => stream!.send(JSON.stringify(message));
	return { canvas, send, resizes };
}

/** The person signs in themselves: a password page opens, they type, the page settles. */
async function signIn(page: Page, canvas: ReturnType<Page['locator']>, send: (message: object) => void) {
	send({ type: 'browser_password_page', visible: true });
	await expect(page.locator('.password-notice')).toBeVisible();
	await canvas.click({ position: { x: 40, y: 40 } });
	await page.keyboard.type('x');
	send({ type: 'browser_password_page', visible: false });
}

const dock = (page: Page) => page.locator('.save-offer');

async function shot(page: Page, size: string, scheme: string, state: string) {
	if (!OUT) return;
	fs.mkdirSync(OUT, { recursive: true });
	await page.mouse.move(2, 2);
	await page.screenshot({ path: path.join(OUT, `${state}-${size}-${scheme}.png`) });
}

test.describe('the pane save-login offer', () => {
	test.describe.configure({ timeout: 120_000 });

	for (const size of SIZES) for (const scheme of ['light', 'dark'] as const) {
		test(`walks every state at ${size.name}, ${scheme}`, async ({ page, request }) => {
			await page.setViewportSize({ width: size.width, height: size.height });
			const { canvas, send, resizes } = await openPane(page, request, scheme);
			await page.waitForTimeout(800);
			const settled = resizes.length;

			server.offer = GITHUB;
			await signIn(page, canvas, send);
			await expect(dock(page)).toBeVisible();
			await expect(dock(page)).toContainText('Save the github.com login for agents?');
			await expect(dock(page)).toContainText('jordan@example.com');
			await expect(dock(page).locator('input, textarea')).toHaveCount(0);
			// Docked below the page: its top edge is at or under the canvas's bottom edge, and it never covers the page.
			const [dockBox, canvasBox] = await Promise.all([dock(page).boundingBox(), canvas.boundingBox()]);
			expect(dockBox!.y).toBeGreaterThanOrEqual(canvasBox!.y + canvasBox!.height - 1);
			if (size.width <= 768) expect(dockBox!.height).toBeGreaterThanOrEqual(44);
			// The agent's viewport did not resize because the dock appeared.
			await page.waitForTimeout(400);
			expect(resizes.length).toBe(settled);
			await shot(page, size.name, scheme, '1-ideal');

			// A failed save keeps the offer, with Save and Not now still there.
			server.save = 500;
			await dock(page).getByRole('button', { name: 'Save' }).click();
			await expect(dock(page)).toContainText('Couldn’t save the login. Try again.');
			await expect(dock(page).getByRole('button', { name: 'Save' })).toBeVisible();
			await expect(dock(page).getByRole('button', { name: 'Not now' })).toBeVisible();
			await shot(page, size.name, scheme, '2-error');

			// A save that works says Saved, fades, and closes; the fade is caught part-way by slowing the page's animations.
			server.save = 200;
			await page.evaluate(() => { (window as { slowClose?: boolean }).slowClose = true; });
			await dock(page).getByRole('button', { name: 'Save' }).click();
			await expect(dock(page)).toContainText('Saved');
			await expect(page.locator('[aria-live="polite"]', { hasText: 'Saved' }).first()).toBeAttached();
			// Pause the fade 100 ms in, so the frame shows it part-way.
			await page.evaluate(() => new Promise<void>((resolve) => {
				const node = document.querySelector('.save-offer')!;
				const look = () => {
					if (!node.classList.contains('fading')) return requestAnimationFrame(look);
					node.getAnimations().forEach((animation) => { animation.pause(); animation.currentTime = 100; });
					resolve();
				};
				look();
			}));
			const opacity = Number(await dock(page).evaluate((node) => getComputedStyle(node).opacity));
			expect(opacity).toBeGreaterThan(0);
			expect(opacity).toBeLessThan(1);
			await shot(page, size.name, scheme, '3-saved-fading');
			await page.evaluate(() => { (window as { slowClose?: boolean }).slowClose = false; document.querySelector('.save-offer')?.getAnimations().forEach((animation) => animation.play()); });
			await expect(dock(page)).toHaveCount(0, { timeout: 15_000 });

			// An update offer.
			server.offer = { ...GITHUB, update: true };
			await signIn(page, canvas, send);
			await expect(dock(page)).toContainText('Update the saved github.com login?');
			await expect(dock(page).getByRole('button', { name: 'Update' })).toBeVisible();
			await shot(page, size.name, scheme, '4-update');
			await dock(page).getByRole('button', { name: 'Not now' }).click();
			await expect(dock(page)).toHaveCount(0);
			expect(server.notNows).toBe(1);

			// A long site wraps its question onto a second line.
			server.offer = { ...GITHUB, domain: LONG_DOMAIN, site: `https://${LONG_DOMAIN}` };
			await signIn(page, canvas, send);
			await expect(dock(page)).toBeVisible();
			const question = dock(page).locator('.question');
			const lineHeight = Number(await question.evaluate((node) => parseFloat(getComputedStyle(node).lineHeight) || parseFloat(getComputedStyle(node).fontSize) * 1.3));
			const height = (await question.boundingBox())!.height;
			// The question never takes more than two lines; on the phone's narrow pane it takes two.
			if (size.width <= 393) expect(height).toBeGreaterThan(lineHeight * 1.5);
			expect(height).toBeLessThan(lineHeight * 2.6);
			await shot(page, size.name, scheme, '5-long-site');
			await dock(page).getByRole('button', { name: 'Not now' }).click();

			// Nothing asked when the agent signed in alone.
			const asked = server.gets;
			send({ type: 'browser_password_page', visible: true });
			send({ type: 'browser_password_page', visible: false });
			await page.waitForTimeout(300);
			expect(server.gets).toBe(asked);
			await expect(dock(page)).toHaveCount(0);
		});
	}

	test('works by keyboard alone: Tab order is Save then Not now, and focus lands on the page when it closes', async ({ page, request }) => {
		await page.setViewportSize({ width: 1440, height: 1000 });
		const { canvas, send } = await openPane(page, request, 'light');
		server.offer = GITHUB;
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
		await page.keyboard.press('Tab');
		expect(await inDock()).toBe('Not now');
		await page.keyboard.press('Enter');
		await expect(dock(page)).toHaveCount(0);
		expect(server.notNows).toBe(1);
		expect(await page.evaluate(() => document.activeElement !== document.body && !!document.activeElement?.closest('.browser-page'))).toBe(true);

		// Save by keyboard: focus is on the page before the Save button is removed.
		server.offer = GITHUB;
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

	test('reduced motion closes the dock without a fade', async ({ page, request }) => {
		await page.emulateMedia({ reducedMotion: 'reduce' });
		await page.setViewportSize({ width: 1440, height: 1000 });
		const { canvas, send } = await openPane(page, request, 'light');
		server.offer = GITHUB;
		await signIn(page, canvas, send);
		await dock(page).getByRole('button', { name: 'Save' }).click();
		await expect(dock(page)).toContainText('Saved');
		await expect(dock(page)).toHaveCount(0, { timeout: 3000 });
		expect(await dock(page).count()).toBe(0);
	});
});
