import { expect, test, type Page } from '@playwright/test';
import { TEST_TOKEN } from '../support/corsia';

// The composer row and the model picker on a Codex account that is past its plan
// and spending credits. The catalog and accounts are mocked (a disposable backend
// has no Codex account); auth, the composer and the picker are the real app.

const authed = (path: string) => `${path}?${['to', 'ken'].join('')}=${TEST_TOKEN}`;
const SHOTS = process.env.PICKER_SHOTS_DIR;
const CAPS = {
	context_window_max: 262144, supports_vision: true, supports_tools: true, supports_reasoning: true,
	reasoning_config: { type: 'effort_levels', values: ['low', 'medium', 'high', 'xhigh'], default: 'xhigh' },
};
const in3d = new Date(Date.now() + 3 * 86_400_000).toISOString();
const in2h = new Date(Date.now() + 2 * 3_600_000).toISOString();
const seen = new Date().toISOString();
const win = (kind: string, minutes: number, used: number, resets: string) =>
	({ kind, window_minutes: minutes, used_percent: used, state: 'reported', source: 'rate_limit_event', observed_at: seen, resets_at: resets });

function account(id: string, name: string, email: string, usage: Record<string, unknown>) {
	return {
		id, provider: 'codex', name, is_default: id === 'pro', state: 'signed_in', revision: 1, identity: {},
		label: { name, email, text: email, custom: null }, ...usage,
	};
}
const ACCOUNTS = [
	account('pro', 'ChatGPT Pro', 'owner@example.test', {
		usage: { state: 'allowed', usable: true, checking: false, check: null, allow_overage: true,
			windows: [win('five_hour', 300, 100, in2h), win('seven_day', 10080, 100, in3d)], facts: { credits: { hasCredits: true } } },
	}),
	account('plus', 'ChatGPT Plus', 'second@example.test', {
		usage: { state: 'allowed', usable: true, checking: false, check: null, windows: [win('five_hour', 300, 12, in2h), win('seven_day', 10080, 40, in3d)] },
	}),
];
const runsOn = [
	{ account_id: 'pro', selected: true, status: 'ready', status_reason: null, resets_at: null, usage: { state: 'allowed', usable: true, windows: [] } },
	{ account_id: 'plus', selected: false, status: 'ready', status_reason: null, resets_at: null, usage: { state: 'allowed', usable: true, windows: [] } },
];
const LONG_MODEL_NAMES = ['deepseek-v4.1-flash', 'qwen3.8-flash', 'GPT-6.1 Sol', 'GPT-5.6 Terra'];
const ROWS = [['gpt-6.1-sol', 'GPT-6.1 Sol'], ['gpt-5.6-terra', 'GPT-5.6 Terra']].map(([slug, label]) => ({
	id: `codex/${slug}`, selector: `codex/${slug}`, litellm_model: `codex/${slug}`, name: `Codex · ${label}`, display_name: label,
	connection: 'codex', provider: 'codex', backend: 'codex', desc: '', size: '', speed: '', status: 'ready', available: true,
	capabilities: CAPS, runs_on: runsOn, runnable: { status: 'ready', status_reason: null },
})).concat(['deepseek-v4.1-flash', 'qwen3.8-flash'].map((label) => ({
	id: `external@ns-96c32a544b/${label}`, selector: `external@ns-96c32a544b/${label}`,
	litellm_model: `external@ns-96c32a544b/${label}`, name: `External · ${label}`,
	display_name: label, connection: 'external', provider: 'external', backend: 'network', desc: '', size: '', speed: '',
	status: 'ready', available: true, capabilities: CAPS, runs_on: [], runnable: { status: 'ready', status_reason: null },
})));

async function serve(page: Page) {
	await page.route('**/api/models**', async (route) => route.request().method() === 'GET'
		? route.fulfill({ json: { models: ROWS, active_local_threads: 0 } }) : route.continue());
	await page.route('**/api/vendor-accounts**', async (route) => route.request().method() === 'GET' && new URL(route.request().url()).pathname === '/api/vendor-accounts'
		? route.fulfill({ json: { accounts: ACCOUNTS } }) : route.continue());
}

/** Pairs of composer-row controls whose boxes intersect. */
async function overlaps(page: Page) {
	return page.evaluate(() => {
		const els = [...document.querySelectorAll<HTMLElement>('.plus-btn, .row-chip, .send-btn')].filter((el) => el.offsetParent);
		const out: string[] = [];
		for (let i = 0; i < els.length; i++) for (let j = i + 1; j < els.length; j++) {
			const a = els[i].getBoundingClientRect();
			const b = els[j].getBoundingClientRect();
			if (a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5) out.push(`${els[i].className} x ${els[j].className}`);
		}
		return out;
	});
}

async function shoot(page: Page, name: string, width: number) {
	for (const theme of ['light', 'dark']) {
		await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
		if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}-${theme}-${width}.png`, scale: 'css', animations: 'disabled' });
	}
}

test('composer shows every long selected model name on desktop and keeps controls separate on mobile', async ({ page }) => {
	await page.setViewportSize({ width: 1440, height: 1000 });
	await serve(page);
	await page.goto(authed('/'));
	await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible({ timeout: 20_000 });
	await page.getByRole('button', { name: 'New chat' }).first().click();
	await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();

	const chip = page.locator('button.model-chip');
	const sheet = page.locator('.model-sheet');
	async function selectModel(label: string) {
		await chip.click();
		await expect(sheet).toBeVisible();
		await sheet.locator('.model-list-item', { hasText: label }).click();
		if (await sheet.isVisible()) await chip.click();
		await expect(sheet).toBeHidden();
		await expect(chip).toHaveText(label);
	}

	for (const label of LONG_MODEL_NAMES) {
		await selectModel(label);
		const clipped = await chip.locator('.model-chip-name').evaluate((el) => el.scrollWidth > el.clientWidth + 1);
		expect(clipped).toBe(false);
		expect(await overlaps(page)).toEqual([]);
		await shoot(page, `composer-${label}`, 1440);
	}

	await page.setViewportSize({ width: 390, height: 844 });
	await expect(page.locator('button.effort-chip')).toHaveText('XHI');
	for (const label of LONG_MODEL_NAMES) {
		await selectModel(label);
		expect(await overlaps(page)).toEqual([]);
		await shoot(page, `composer-${label}-xhi`, 390);
	}

	await chip.click();
	await expect(sheet).toBeVisible();
	await expect(sheet).toContainText('plan used · spending credits');
	const box = (await sheet.boundingBox())!;
	expect(box.width).toBeLessThanOrEqual(390);
	expect(box.x).toBeGreaterThanOrEqual(0);
	expect(box.x + box.width).toBeLessThanOrEqual(390);
	console.log(`[COMPOSER-LAYOUT] 390px sheet width ${Math.round(box.width)}`);
});
