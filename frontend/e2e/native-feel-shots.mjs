// One-shot visual matrix for the native-feel pass (not a test — a review
// artifact generator). Boots against the already-running mock stack
// (vite :5178 → backend :3110) and writes screenshots to .qa/nf-shots/.
import { chromium } from '@playwright/test';
import { mkdirSync } from 'node:fs';

const BASE = 'http://localhost:5178';
const TOKEN = process.env.CORSIA_E2E_TOKEN;
if (!TOKEN) {
	throw new Error(
		'CORSIA_E2E_TOKEN is not set. Seed a disposable test account and export its API token before running this script.',
	);
}
const OUT = '.qa/nf-shots';
mkdirSync(OUT, { recursive: true });

const VIEWPORTS = {
	desktop: { width: 1280, height: 820 },
	mobile: { width: 390, height: 844 },
};

const browser = await chromium.launch();

async function shoot(vpName, theme) {
	const ctx = await browser.newContext({
		viewport: VIEWPORTS[vpName],
		deviceScaleFactor: 2,
		isMobile: vpName === 'mobile',
		hasTouch: vpName === 'mobile',
	});
	const page = await ctx.newPage();
	await page.goto(`${BASE}/?token=${encodeURIComponent(TOKEN)}`);
	await page.evaluate((t) => localStorage.setItem('corsia_theme', t), theme);
	await page.reload();
	await page.waitForSelector('.composer', { timeout: 15000 });
	await page.waitForTimeout(800);

	const tag = `${vpName}-${theme}`;
	await page.screenshot({ path: `${OUT}/${tag}-1-home.png` });

	// Palette
	await page.keyboard.press(process.platform === 'darwin' ? 'Meta+k' : 'Control+k');
	await page.waitForTimeout(600);
	await page.screenshot({ path: `${OUT}/${tag}-2-palette.png` });
	await page.keyboard.press('Escape');
	await page.waitForTimeout(300);

	// Chat: send a message against the mock model, catch the live stream.
	await page.locator('.composer').click();
	await page.keyboard.type('Hello! Give me a long answer.');
	await page.keyboard.press('Enter');
	await page.waitForTimeout(1200);
	await page.screenshot({ path: `${OUT}/${tag}-3-chat-stream.png` });
	await page.waitForTimeout(2500);
	await page.screenshot({ path: `${OUT}/${tag}-4-chat-done.png` });

	// Settings
	if (vpName === 'mobile') {
		// open the drawer first (hamburger = first header button)
		await page.locator('.shell-header .h-icon').first().click();
		await page.waitForTimeout(600);
		await page.screenshot({ path: `${OUT}/${tag}-5-drawer.png` });
	}
	await page.locator('.rail-link', { hasText: 'Settings' }).click();
	await page.waitForTimeout(800);
	await page.screenshot({ path: `${OUT}/${tag}-6-settings.png` });

	await ctx.close();
	console.log(`done: ${tag}`);
}

for (const vp of ['desktop', 'mobile']) {
	for (const theme of ['dark', 'light']) {
		await shoot(vp, theme);
	}
}
await browser.close();
console.log('matrix complete');
