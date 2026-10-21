import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { TEST_TOKEN } from '../support/corsia';

// The model picker groups /api/models rows by account. The catalog and the vendor accounts are
// mocked (a disposable backend has no Claude/Codex account to limit); auth, threads and the
// picker itself are the real app against the disposable backend. As /api/models ships it, a
// harness row's account decision rides on its `runs_on` entry, and what an account has left is
// the vendor-accounts record. An account-backed group (a harness row with `runs_on`) keeps every
// model row visible and heads them with `.picker-account-header`: its `.usage` carries the
// account's figures (a button when tapping it opens the `.picker-account-usage` breakdown) or, when
// nobody can pick from it, says so in words; this is intended (InputBarModelSheet.svelte: account-
// backed rows stay visible, the account list opens on demand). A local server head is static and a
// network group without accounts keeps the collapsible `.model-group-head`. A row its selected
// account blocks carries a red `.row-note`.

// The disposable account's API credential rides on the query string, as in the other flows.
const authed = (path: string) => `${path}?${['to', 'ken'].join('')}=${TEST_TOKEN}`;
const SHOTS = process.env.PICKER_SHOTS_DIR;
const CAPS = {
	context_window_max: 262144,
	supports_vision: true,
	supports_tools: true,
	supports_reasoning: true,
	reasoning_config: { type: 'effort_levels', values: ['low', 'medium', 'high'], default: 'medium' },
};
const in3d = new Date(Date.now() + 3 * 86_400_000).toISOString();
const in2h = new Date(Date.now() + 2 * 3_600_000).toISOString();
const seen = new Date().toISOString();

type Win = { kind: string; window_minutes: number; used_percent: number | null; state?: string; resets_at?: string; scope_model?: string };
const windows = (list: Win[]) => list.map((w) => ({ state: 'reported', source: 'rate_limit_event', observed_at: seen, resets_at: in3d, ...w }));
const usage = (list: Win[]) => ({ state: 'allowed', usable: true, windows: windows(list) });
const limitedUsage = (list: Win[], resetsAt = in3d) => ({ state: 'limited', usable: false, resets_at: resetsAt, windows: windows(list) });
const WEEKLY = [{ kind: 'seven_day', window_minutes: 10080, used_percent: 71 }];
const FIVE_AND_WEEK = [
	{ kind: 'five_hour', window_minutes: 300, used_percent: 42, resets_at: in2h },
	...WEEKLY,
];

/** A signed-in vendor account as `/api/vendor-accounts` returns it. */
const account = (id: string, provider: string, accountUsage: Record<string, unknown>) => ({
	id, provider, name: `${id} test`, is_default: false, state: 'signed_in', revision: 1, identity: {},
	label: { name: 'Test account', email: null, text: 'Test account', custom: null },
	usage: { checking: false, check: null, ...accountUsage },
});

/** The selected account's decision for a row, as `runs_on` ships it. */
const entry = (acct: ReturnType<typeof account>, over: Record<string, unknown> = {}) => ({
	account_id: acct.id, selected: true, status: 'ready', status_reason: null, resets_at: null, usage: acct.usage, ...over,
});

type Acct = ReturnType<typeof account>;
function harnessRow(harness: 'claude-code' | 'codex', slug: string, label: string, acct: Acct, entryOver: Record<string, unknown> = {}) {
	const prefix = harness === 'codex' ? 'Codex' : 'Claude Code';
	return {
		id: `${harness}/${slug}`, selector: `${harness}/${slug}`, litellm_model: `${harness}/${slug}`,
		name: `${prefix} · ${label}`, display_name: label,
		connection: harness, provider: harness, backend: harness === 'codex' ? 'codex' : 'cc',
		desc: '', size: '', speed: '', status: 'ready', available: true, capabilities: CAPS,
		runs_on: [entry(acct, entryOver)],
	};
}

// One vendor account per harness: its windows decide the group's figures, its `runs_on` entry
// decides each model. The account's Opus-scoped weekly window is consumed, so the backend
// blocks the Opus row alone; a ready row carries no per-model percentage.
const ccAcct = account('acct-cc', 'claude-code', usage([...FIVE_AND_WEEK, { kind: 'seven_day_opus', window_minutes: 10080, used_percent: 92, scope_model: 'opus' }]));
const cxAcct = account('acct-cx', 'codex', usage(WEEKLY));
const opusLimited = { status: 'unavailable', status_reason: 'usage_limited', resets_at: in2h, usage: limitedUsage([{ kind: 'seven_day_opus', window_minutes: 10080, used_percent: 92, scope_model: 'opus' }], in2h) };
const claude = (entryOver = {}, sonnetOver = {}) => [
	harnessRow('claude-code', 'opus', 'Opus 5.5', ccAcct, { ...opusLimited, ...entryOver }),
	harnessRow('claude-code', 'sonnet', 'Sonnet 5.5', ccAcct, sonnetOver),
];
const ccLockedOut = account('acct-cc', 'claude-code', limitedUsage([{ kind: 'seven_day', window_minutes: 10080, used_percent: 100 }]));
const claudeLimited = () => [
	harnessRow('claude-code', 'opus', 'Opus 5.5', ccLockedOut, { status: 'unavailable', status_reason: 'usage_limited', resets_at: in3d }),
	harnessRow('claude-code', 'sonnet', 'Sonnet 5.5', ccLockedOut, { status: 'unavailable', status_reason: 'usage_limited', resets_at: in3d }),
];
const claudeSignedOut = () => [
	harnessRow('claude-code', 'opus', 'Opus 5.5', ccAcct, { status: 'unavailable', status_reason: 'connection_unavailable', usage: { state: 'unknown', usable: true, windows: [] } }),
	harnessRow('claude-code', 'sonnet', 'Sonnet 5.5', ccAcct, { status: 'unavailable', status_reason: 'connection_unavailable', usage: { state: 'unknown', usable: true, windows: [] } }),
];
const codex = (entryOver = {}) => [
	harnessRow('codex', 'sol', 'GPT-5.6 Sol', cxAcct, entryOver),
	harnessRow('codex', 'terra', 'GPT-5.6 Terra', cxAcct, entryOver),
];
const local = () => ['Qwen 3.8 Flash', 'DeepSeek V4.1 Flash'].map((label, i) => ({
	id: `omlx-${i}`, selector: `omlx@srv/${i}`, litellm_model: `omlx@srv/${i}`, name: label, display_name: label,
	connection: 'omlx', provider: 'omlx', backend: 'network', server_name: 'This Mac',
	desc: '', size: '', speed: '', status: 'ready', is_local: true, capabilities: CAPS,
}));

async function serve(page: Page, rows: unknown[], accounts?: unknown[]) {
	await page.route('**/api/models**', async (route) => {
		if (route.request().method() === 'GET') await route.fulfill({ json: { models: rows, active_local_threads: 0 } });
		else await route.continue();
	});
	if (accounts !== undefined) {
		await page.route('**/api/vendor-accounts**', async (route) => {
			if (route.request().method() === 'GET' && new URL(route.request().url()).pathname === '/api/vendor-accounts') {
				await route.fulfill({ json: { accounts } });
			} else await route.continue();
		});
	}
}

async function openNewChatPicker(page: Page) {
	await page.goto(authed('/'));
	await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible({ timeout: 20_000 });
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByRole('button', { name: 'New chat' }).first().click();
	await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
	await page.locator('button.model-chip').click();
	await expect(page.locator('.model-sheet')).toBeVisible();
}

async function openThreadPicker(page: Page, title: string) {
	await page.goto(authed('/'));
	await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible({ timeout: 20_000 });
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-title', { hasText: title }).click({ timeout: 10_000 });
	await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible({ timeout: 20_000 });
	await page.locator('button.model-chip').click();
	await expect(page.locator('.model-sheet')).toBeVisible();
}

/** No name or status text is clipped, and the sheet stays inside the viewport. */
async function expectNothingTruncated(page: Page) {
	const clipped = await page.evaluate(() => {
		const out: string[] = [];
		for (const el of document.querySelectorAll<HTMLElement>('.model-sheet .group-name, .model-sheet .group-status, .model-sheet .picker-account-title, .model-sheet .picker-account-header .usage, .model-sheet .model-list-name, .model-sheet .row-note, .model-sheet .picker-account-usage div')) {
			if (el.scrollWidth > el.clientWidth + 1) out.push(el.textContent ?? '');
		}
		const sheet = document.querySelector('.model-sheet')!.getBoundingClientRect();
		if (sheet.right > window.innerWidth + 1 || sheet.left < -1) out.push('sheet outside viewport');
		return out;
	});
	expect(clipped).toEqual([]);
}

async function shoot(page: Page, name: string, testInfo: { project: { name: string } }) {
	const size = testInfo.project.name === 'chromium-mobile' ? '390x844' : '1440x900';
	const heightPct = await page.evaluate(() => Math.round((document.querySelector('.model-sheet')!.getBoundingClientRect().height / window.innerHeight) * 1000) / 10);
	console.log(`[PICKER-GROUPS] ${name} ${size} sheet=${heightPct}% of viewport`);
	if (SHOTS) await page.screenshot({ path: `${SHOTS}/${name}-${size}.png`, scale: 'css', animations: 'disabled' });
	return heightPct;
}

const head = (page: Page, name: string) => page.locator(`section[aria-label="${name}"] .model-group-head`);
/** An account-backed group's figures (or its words when nobody can pick from it), in its account header. */
const figures = (page: Page, name: string) => page.locator(`section[aria-label="${name}"] .picker-account-header .usage`);
const groupRows = (page: Page, name: string) => page.locator(`section[aria-label="${name}"] .model-list-item`);

test.describe('model picker grouped by account', () => {
	test('ready: each account lists its models with its figures, a tap on the figures opens the breakdown', async ({ page }, testInfo) => {
		await serve(page, [...claude(), ...codex(), ...local()], [ccAcct, cxAcct]);
		await openNewChatPicker(page);
		await expect(head(page, 'This Mac')).not.toHaveAttribute('aria-expanded', /.*/); // a local server head is static
		await expect(head(page, 'This Mac').locator('.group-status')).toHaveCount(0);
		await expect(figures(page, 'Claude Code')).toContainText('5h 58% left · wk 29% left');
		await expect(figures(page, 'Codex')).toHaveText('wk 29% left');
		await expect(groupRows(page, 'Codex')).toHaveCount(2);
		await expect(page.locator('.model-list-item')).toHaveCount(6);
		await expect(page.locator('.row-note', { hasText: /^limited · back / })).toHaveCount(1); // the Opus row's own limit
		await expect(page.locator('.model-list-name', { hasText: '·' })).toHaveCount(0); // a row carries its model name, not the provider prefix
		await expectNothingTruncated(page);
		await shoot(page, 'ready-all-open', testInfo);
		await figures(page, 'Claude Code').click();
		await expect(page.locator('section[aria-label="Claude Code"] .picker-account-usage div')).toHaveCount(2);
		await expectNothingTruncated(page);
		await shoot(page, 'ready-usage-breakdown', testInfo);
		// Cross-provider consistency: harness and local groups share one row shape and the same capability icons.
		const shape = await page.evaluate(() => {
			const name = (el: Element) => (el.getAttribute('class') ?? '').split(' ')[0];
			return [...document.querySelectorAll('.model-sheet .model-group')].map((group) => ({
				rows: [...group.querySelectorAll('.model-list-item')].map((row) => ({
					parts: [...row.children].map(name).filter((part) => part !== 'row-note' && part !== 'check-glyph'),
					caps: [...row.querySelectorAll('.model-caps [aria-label]')].map((icon) => icon.getAttribute('aria-label')),
				})),
			}));
		});
		expect(shape).toHaveLength(3);
		for (const group of shape) {
			for (const row of group.rows) {
				expect(row.caps).toEqual(['Reasoning', 'Vision', 'Tools']);
				expect(row.parts).toEqual(shape[0].rows[1].parts);
			}
		}
	});

	test('limited: the account says so with its reset and its models stay listed, disabled', async ({ page }, testInfo) => {
		await serve(page, [...claudeLimited(), ...codex(), ...local()], [ccLockedOut, cxAcct]);
		await openNewChatPicker(page);
		const limited = page.locator('section[data-state="limited"]');
		await expect(limited).toHaveCount(1);
		await expect(limited.locator('.picker-account-header .usage')).toHaveText(/^limited · back /);
		await expect(limited.locator('.model-list-item')).toHaveCount(2);
		for (const item of await limited.locator('.model-list-item').all()) await expect(item).toBeDisabled();
		await expectNothingTruncated(page);
		await shoot(page, 'limited', testInfo);
	});

	test('limited, thread bound to the account: its model stays current and disabled', async ({ page, request }, testInfo) => {
		const title = `picker bound ${Date.now()}`;
		const created = await request.post(authed('/api/threads'), { data: { title, model: 'claude-code/opus' } });
		expect(created.ok()).toBeTruthy();
		const threadId = (await created.json()).thread_id as string;
		await serve(page, [...claudeLimited(), ...codex(), ...local()], [ccLockedOut, cxAcct]);
		await openThreadPicker(page, title);
		const limited = page.locator('section[data-state="limited"]');
		const bound = limited.locator('.model-list-item', { hasText: 'Opus 5.5' });
		await expect(bound).toBeDisabled();
		await expect(bound.getByRole('img', { name: 'Current' })).toBeVisible();
		await expect(limited.locator('.model-list-item', { hasText: 'Sonnet 5.5' }).getByRole('img', { name: 'Current' })).toHaveCount(0);
		await expect(limited.locator('.picker-account-header .usage')).toHaveText(/^limited · back /);
		await expectNothingTruncated(page);
		await shoot(page, 'limited-thread-bound', testInfo);
		const listed = await (await request.get(authed('/api/threads'))).json();
		const rows = (Array.isArray(listed) ? listed : listed.threads) as Array<{ id: string; model: string | null }>;
		expect(rows.find((thread) => thread.id === threadId)?.model).toBe('claude-code/opus');
	});

	test('signed out: the account says so, offers Sign in, and its models are disabled', async ({ page }, testInfo) => {
		const ccSignedOutAccount = { ...ccAcct, state: 'signed_out', usage: { checking: false, check: null, state: 'unknown', usable: true, windows: [] } };
		await serve(page, [...claudeSignedOut(), ...codex(), ...local()], [ccSignedOutAccount, cxAcct]);
		await openNewChatPicker(page);
		const signedOut = page.locator('section[data-state="signed_out"]');
		await expect(signedOut.locator('.picker-account-header .usage')).toHaveText('signed out');
		await expect(signedOut.getByRole('button', { name: 'Sign in' })).toBeVisible();
		await expect(signedOut.locator('.model-list-item')).toHaveCount(2);
		for (const item of await signedOut.locator('.model-list-item').all()) await expect(item).toBeDisabled();
		await expectNothingTruncated(page);
		await shoot(page, 'signed-out', testInfo);
	});

	test('stale usage: a window with state unknown reads usage unknown, never a percentage', async ({ page }, testInfo) => {
		const ccStale = account('acct-cc', 'claude-code', usage([{ kind: 'seven_day', window_minutes: 10080, used_percent: 88, state: 'unknown' }]));
		const stale = [
			harnessRow('claude-code', 'opus', 'Opus 5.5', ccStale),
			harnessRow('claude-code', 'sonnet', 'Sonnet 5.5', ccStale),
		];
		await serve(page, [...stale, ...codex(), ...local()], [ccStale, cxAcct]);
		await openNewChatPicker(page);
		await expect(figures(page, 'Claude Code')).toHaveText('usage unknown');
		await expect(page.locator('.model-sheet')).not.toContainText('88%');
		await expectNothingTruncated(page);
		await shoot(page, 'stale-usage', testInfo);
	});

	test('local only: one server header that reads ready, no meter', async ({ page }, testInfo) => {
		await serve(page, local());
		await openNewChatPicker(page);
		await expect(page.locator('.model-group')).toHaveCount(1);
		await expect(head(page, 'This Mac').locator('.group-status')).toHaveCount(0);
		await expect(page.locator('.model-list-item')).toHaveCount(2);
		await expectNothingTruncated(page);
		await shoot(page, 'local-only', testInfo);
	});

	test('usage levels: normal, amber and red segments, a red limited header, both numbers in the breakdown, readable in both themes', async ({ page }, testInfo) => {
		const external = (label: string, over: Record<string, unknown> = {}) => ({
			id: `ext-${label}`, selector: `external@ns/${label}`, litellm_model: `external@ns/${label}`, name: label, display_name: label,
			connection: 'external', provider: 'external', backend: 'network', server_name: 'Backup account',
			desc: '', size: '', speed: '', status: 'unavailable', status_reason: 'usage_limited', status_resets_at: in3d, capabilities: CAPS, ...over,
		});
		const ccLevels = account('acct-cc', 'claude-code', usage([
			{ kind: 'five_hour', window_minutes: 300, used_percent: 42, resets_at: in2h },
			{ kind: 'seven_day', window_minutes: 10080, used_percent: 92 },
			{ kind: 'seven_day_opus', window_minutes: 10080, used_percent: 96, scope_model: 'opus' },
		]));
		const cxLevels = account('acct-cx', 'codex', usage([{ kind: 'seven_day', window_minutes: 10080, used_percent: 80 }]));
		await serve(page, [
			harnessRow('claude-code', 'opus', 'Opus 5.5', ccLevels, { status: 'unavailable', status_reason: 'usage_limited', resets_at: in3d, usage: limitedUsage([{ kind: 'seven_day_opus', window_minutes: 10080, used_percent: 96, scope_model: 'opus' }]) }),
			harnessRow('claude-code', 'sonnet', 'Sonnet 5.5', ccLevels),
			harnessRow('codex', 'sol', 'GPT-5.6 Sol', cxLevels),
			harnessRow('codex', 'terra', 'GPT-5.6 Terra', cxLevels),
			external('Qwen 3.8 Flash'),
			...local(),
		], [ccLevels, cxLevels]);
		await openNewChatPicker(page);
		// A figure is every odd child of `.usage`; the even ones are the ' · ' separators.
		const segs = (name: string) => figures(page, name).locator('> span:nth-child(odd)');
		await expect(segs('Claude Code')).toHaveText(['5h 58% left', 'wk 8% left']);
		await expect(segs('Claude Code').nth(0)).not.toHaveClass(/\b(amber|red)\b/);
		await expect(segs('Claude Code').nth(1)).toHaveClass(/\bred\b/);
		await expect(segs('Codex')).toHaveText(['wk 20% left']);
		await expect(segs('Codex').nth(0)).toHaveClass(/\bamber\b/);
		await expect(page.locator('section[data-state="limited"] .group-status > span')).toHaveClass(/usage-red/);
		await expect(head(page, 'This Mac').locator('.group-status')).toHaveCount(0);
		await expect(figures(page, 'Claude Code')).toHaveAttribute('aria-label', '5-hour: 58% left, weekly: 8% left');
		await expect(figures(page, 'Codex')).toHaveAttribute('aria-label', 'weekly: 20% left');
		const opusNote = page.locator('.row-note', { hasText: /^limited · back / });
		await expect(opusNote).toHaveClass(/usage-red/);
		await expectNothingTruncated(page);
		await shoot(page, 'usage-levels', testInfo);
		for (const name of ['Claude Code', 'Codex']) await figures(page, name).click();
		await expect(page.locator('.picker-account-usage div')).toHaveCount(3);
		await expect(page.locator('section[aria-label="Claude Code"] .picker-account-usage div').nth(0)).toHaveText(/^5h · 58% left · 42% used · resets /);
		await expect(page.locator('section[aria-label="Claude Code"] .picker-account-usage div').nth(1)).toHaveText(/^wk · 8% left · 92% used · resets /);
		await expect(page.locator('section[aria-label="Codex"] .picker-account-usage div').nth(0)).toHaveText(/^wk · 20% left · 80% used · resets /);
		await expectNothingTruncated(page);
		await shoot(page, 'usage-levels-breakdown', testInfo);

		// WCAG contrast of every colored usage text against the sheet background it sits on, in both themes.
		for (const theme of ['dark', 'light']) {
			await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
			await shoot(page, `usage-levels-${theme}`, testInfo);
			const ratios = await page.evaluate(() => {
				const rgb = (css: string) => {
					const ctx = document.createElement('canvas').getContext('2d')!;
					ctx.clearRect(0, 0, 1, 1);
					ctx.fillStyle = css;
					ctx.fillRect(0, 0, 1, 1);
					const [r, g, b, a] = ctx.getImageData(0, 0, 1, 1).data;
					return { r, g, b, a: a / 255 };
				};
				const backdrop = (el: Element) => {
					for (let node: Element | null = el; node; node = node.parentElement) {
						const c = rgb(getComputedStyle(node).backgroundColor);
						if (c.a === 1) return c;
					}
					return rgb(getComputedStyle(document.body).backgroundColor);
				};
				const lum = ({ r, g, b }: { r: number; g: number; b: number }) => {
					const f = (v: number) => { const s = v / 255; return s <= 0.03928 ? s / 12.92 : ((s + 0.055) / 1.055) ** 2.4; };
					return 0.2126 * f(r) + 0.7152 * f(g) + 0.0722 * f(b);
				};
				const out: Record<string, { color: string; background: string; ratio: number }> = {};
				const probes: Array<[string, string]> = [
					['amber', '.picker-account-header .usage .amber'], ['red', '.picker-account-header .usage .red'], ['red row note', '.row-note.usage-red'],
					['red limited header', 'section[data-state="limited"] .group-status .usage-red'],
				];
				for (const [label, selector] of probes) {
					const el = document.querySelector(`.model-sheet ${selector}`)!;
					const fg = rgb(getComputedStyle(el).color);
					const bg = backdrop(el);
					const [hi, lo] = [lum(fg), lum(bg)].sort((a, b) => b - a);
					out[label] = { color: `rgb(${fg.r}, ${fg.g}, ${fg.b})`, background: `rgb(${bg.r}, ${bg.g}, ${bg.b})`, ratio: Math.round(((hi + 0.05) / (lo + 0.05)) * 100) / 100 };
				}
				return out;
			});
			console.log(`[PICKER-CONTRAST] ${theme} ${JSON.stringify(ratios)}`);
			for (const [label, measured] of Object.entries(ratios)) expect(measured.ratio, `${theme} ${label}`).toBeGreaterThanOrEqual(4.5);
		}
	});

	test('mixed fixture: ready Claude Code, limited Codex, ready External and local; External collapsed then expanded', async ({ page }, testInfo) => {
		const external = [{
			id: 'ext-0', selector: 'external@ns/qwen', litellm_model: 'external@ns/qwen', name: 'Qwen 3.8 Flash', display_name: 'Qwen 3.8 Flash',
			connection: 'external', provider: 'external', backend: 'network', server_name: 'External',
			desc: '', size: '', speed: '', status: 'ready', capabilities: CAPS,
		}];
		// The account is the one that is limited, so the account and its rows agree.
		const cxLimited = account('acct-cx', 'codex', limitedUsage(WEEKLY));
		const codexLimited = [
			harnessRow('codex', 'sol', 'GPT-5.6 Sol', cxLimited, { status: 'unavailable', status_reason: 'usage_limited', resets_at: in3d }),
			harnessRow('codex', 'terra', 'GPT-5.6 Terra', cxLimited, { status: 'unavailable', status_reason: 'usage_limited', resets_at: in3d }),
		];
		await serve(page, [...claude({}), ...codexLimited, ...external, ...local()], [ccAcct, cxLimited]);
		await openNewChatPicker(page);
		await expect(head(page, 'External')).toHaveAttribute('aria-expanded', 'true');
		await expect(figures(page, 'Codex')).toHaveText(/^limited · back /);
		await expect(groupRows(page, 'Codex')).toHaveCount(2);
		await expect(page.locator('.model-list-item')).toHaveCount(7);
		await expect(page.locator('section[aria-label="Codex"] .model-list-item:not(:disabled)')).toHaveCount(0);
		await shoot(page, 'mixed-open', testInfo);
		await head(page, 'External').click();
		await expect(head(page, 'External')).toHaveAttribute('aria-expanded', 'false');
		await expect(page.locator('.model-list-item')).toHaveCount(6);
		await shoot(page, 'mixed-external-collapsed', testInfo);
		await head(page, 'External').click();
		await expect(page.locator('.model-list-item')).toHaveCount(7);
		await expectNothingTruncated(page);
	});
});
