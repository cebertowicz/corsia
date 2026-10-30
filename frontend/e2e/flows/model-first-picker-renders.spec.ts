import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { TEST_TOKEN } from '../support/corsia';

const shots = process.env.PICKER_SHOTS_DIR;
const authed = (path: string) => `${path}?${['to', 'ken'].join('')}=${TEST_TOKEN}`;
const at = new Date(Date.now() + 3 * 86_400_000).toISOString();
const CAPS = { context_window_max: 262144, supports_vision: true, supports_tools: true, supports_reasoning: true, reasoning_config: { type: 'effort_levels' as const, values: ['low', 'medium', 'high'], default: 'medium' } };
type Account = ReturnType<typeof account>;

function usage(fiveHourPercent = 12, weekPercent = fiveHourPercent) { return { state: 'allowed', usable: true, windows: [{ kind: 'five_hour', state: 'reported', source: 'fixture', observed_at: at, resets_at: at, window_minutes: 300, used_percent: fiveHourPercent }, { kind: 'seven_day', state: 'reported', source: 'fixture', observed_at: at, resets_at: at, window_minutes: 10080, used_percent: weekPercent }] }; }
function limited() { return { state: 'limited', usable: false, resets_at: at, windows: [] }; }
function account(id: string, label: string, provider: 'claude-code' | 'codex', state = usage()) { return { id, provider, name: label, is_default: false, state: 'signed_in', revision: 1, identity: {}, label: { name: label, email: `${id}@example.test`, text: label, custom: null }, usage: { checking: false, check: null, ...state } }; }
function entry(account: Account, selected = false, over: Record<string, unknown> = {}) { return { account_id: account.id, selected, status: 'ready', status_reason: null, resets_at: null, usage: account.usage, ...over }; }
function models(provider: 'claude-code' | 'codex', accounts: Account[], entries = accounts.map((item, index) => entry(item, index === 0))) {
	const names = provider === 'claude-code' ? [['Opus 5.5', 'opus'], ['Sonnet 5.5', 'sonnet'], ['Haiku 4.5', 'haiku']] : [['GPT-6.1 Sol', 'gpt-6.1-sol'], ['GPT-5.6 Terra', 'gpt-5.6-terra']];
	const prefix = provider === 'codex' ? 'Codex' : 'Claude Code';
	return names.map(([label, slug]) => ({ id: `${provider}/${slug}`, selector: `${provider}/${slug}`, litellm_model: `${provider}/${slug}`, name: `${prefix} · ${label}`, display_name: label, connection: provider, provider, backend: provider === 'codex' ? 'codex' : 'cc', desc: '', size: '', speed: '', status: 'ready', available: true, capabilities: CAPS, runs_on: entries }));
}
function local() { return [{ id: 'omlx/local', selector: 'omlx@local', litellm_model: 'omlx@local', name: 'Local', display_name: 'Local', connection: 'omlx', provider: 'omlx', backend: 'network', server_name: 'This Mac', desc: '', size: '', speed: '', status: 'ready', available: true, capabilities: CAPS }]; }
function external() { return [{ id: 'external/qwen3.8-flash', selector: 'external@qwen3.8-flash', litellm_model: 'external@qwen3.8-flash', name: 'Alibaba · Qwen 3.8 Flash', display_name: 'Qwen 3.8 Flash', connection: 'external', provider: 'external', backend: 'external', desc: '', size: '', speed: '', status: 'ready', available: true, capabilities: CAPS }]; }

async function routePicker(page: Page, current: () => { rows: unknown[]; accounts: unknown[]; policy?: unknown; sources?: unknown[] }) {
	await page.route('**/api/models**', (route) => route.request().method() === 'GET' ? route.fulfill({ json: { models: current().rows, active_local_threads: 0 } }) : route.continue());
	await page.route('**/api/vendor-accounts**', (route) => route.request().method() === 'GET' ? route.fulfill({ json: { accounts: current().accounts } }) : route.continue());
	await page.route('**/api/provider-policy**', (route) => route.fulfill({ json: current().policy ?? { providers: [] } }));
	await page.route('**/api/providers/sources**', (route) => route.fulfill({ json: { sources: current().sources ?? [] } }));
}
async function openPicker(page: Page, title?: string) {
	await page.goto(authed('/'));
	await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible({ timeout: 20_000 });
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	if (title) await page.locator('.thread-title', { hasText: title }).click({ timeout: 10_000 });
	else await page.getByRole('button', { name: 'New chat' }).first().click();
	await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
	await page.locator('button.model-chip').click();
	await expect(page.locator('.model-sheet')).toBeVisible();
}
async function capture(page: Page, id: string, prepare?: () => Promise<void>) {
	for (const [device, width, height] of [['desktop', 1440, 1000], ['phone', 390, 844]] as const) {
		await page.setViewportSize({ width, height });
		await prepare?.();
		for (const theme of ['light', 'dark']) {
			await page.evaluate((value) => document.documentElement.setAttribute('data-theme', value), theme);
			const layout = await page.evaluate((scenario) => {
				const sheet = document.querySelector<HTMLElement>('.model-sheet')!;
				const clipped = [...document.querySelectorAll<HTMLElement>('.model-sheet .model-list-name, .picker-account-header, .account-usage-row .figures')].filter((el) => el.scrollWidth > el.clientWidth + 1).map((el) => el.textContent);
				const controls = [...document.querySelectorAll<HTMLElement>('.model-sheet button')].map((el) => ({ label: el.textContent?.trim() ?? '', rect: el.getBoundingClientRect() }));
				const overlaps: string[] = [];
				for (let left = 0; left < controls.length; left += 1) for (let right = left + 1; right < controls.length; right += 1) {
					const a = controls[left].rect; const b = controls[right].rect;
					if (Math.min(a.right, b.right) > Math.max(a.left, b.left) && Math.min(a.bottom, b.bottom) > Math.max(a.top, b.top)) overlaps.push(`${controls[left].label}/${controls[right].label}`);
				}
				const gutterOffset = scenario !== 'M4b' ? 0 : (() => {
					const dot = document.querySelector<HTMLElement>('section[aria-label="Claude Code"] .model-status-dot');
					const order = document.querySelector<HTMLElement>('section[aria-label="Claude Code"] .account-usage-row .order');
					return dot && order ? Math.round(dot.getBoundingClientRect().left - order.getBoundingClientRect().left) : Number.NaN;
				})();
				const wrappedVendors = [...document.querySelectorAll<HTMLElement>('.picker-account-header .vendor')].filter((el) => {
					const range = document.createRange();
					range.selectNodeContents(el);
					return new Set([...range.getClientRects()].map((rect) => Math.round(rect.top))).size > 1;
				}).map((el) => el.textContent);
				// The current model's and the current account's checks share one gutter column, clear of the dot and order beside them.
				const accountCheck = document.querySelector<HTMLElement>('.model-sheet .account-usage-row .check-glyph')?.getBoundingClientRect();
				const modelCheck = document.querySelector<HTMLElement>('.model-sheet .model-list-item .check-glyph')?.getBoundingClientRect();
				const checkColumnOffset = accountCheck && modelCheck ? Math.round(accountCheck.left - modelCheck.left) : 0;
				const checkOverlaps = [...document.querySelectorAll<HTMLElement>('.model-sheet .check-glyph')].filter((check) => {
					const next = check.parentElement!.querySelector<HTMLElement>('.model-status-dot, .order, .picker-label');
					return next ? check.getBoundingClientRect().right > next.getBoundingClientRect().left : false;
				}).length;
				// The check sits on its row's first line, even when the row wraps its figures onto a second line.
				const checkRowDrift = Math.max(0, ...[...document.querySelectorAll<HTMLElement>('.model-sheet .check-glyph')].map((check) => {
					const row = check.parentElement!;
					const mark = row.querySelector<HTMLElement>('.model-status-dot, .order');
					const label = row.querySelector<HTMLElement>('.picker-label, .model-list-name');
					const text = label ? document.createTreeWalker(label, NodeFilter.SHOW_TEXT, { acceptNode: (node) => (node.textContent?.trim() ? NodeFilter.FILTER_ACCEPT : NodeFilter.FILTER_REJECT) }).nextNode() : null;
					const range = document.createRange();
					if (text) { range.setStart(text, 0); range.setEnd(text, 1); }
					const anchor = mark?.getBoundingClientRect() ?? (text ? range.getClientRects()[0] : undefined);
					const box = check.getBoundingClientRect();
					return anchor ? Math.abs((box.top + box.bottom) / 2 - (anchor.top + anchor.bottom) / 2) : Number.POSITIVE_INFINITY;
				}));
				// The chooser caret is a drawn chevron centred on its title line, not a text glyph riding the baseline.
				const caretDrift = Math.max(0, ...[...document.querySelectorAll<HTMLElement>('.model-sheet button.picker-account-title')].map((title) => {
					const caret = title.querySelector('.chooser-caret svg')?.getBoundingClientRect();
					const vendor = title.querySelector('.vendor')!.getBoundingClientRect();
					return caret ? Math.abs((caret.top + caret.bottom) / 2 - (vendor.top + vendor.bottom) / 2) : Number.POSITIVE_INFINITY;
				}));
				return { clipped, overflows: sheet.scrollWidth > sheet.clientWidth, overlaps, gutterOffset, checkColumnOffset, checkOverlaps, checkRowDrift, caretDrift, wrappedVendors };
			}, id);
			expect(layout.clipped).toEqual([]);
			if (device === 'phone') expect(layout.overflows).toBe(false);
			expect(layout.overlaps).toEqual([]);
			expect(layout.wrappedVendors).toEqual([]);
			expect(layout.gutterOffset).toBe(0);
			expect(layout.checkColumnOffset).toBe(0);
			expect(layout.checkOverlaps).toBe(0);
			expect(layout.checkRowDrift).toBeLessThanOrEqual(2);
			expect(layout.caretDrift).toBeLessThanOrEqual(1);
			if (shots) await page.screenshot({ path: `${shots}/${id}-${device}-${theme}.png`, scale: 'css', animations: 'disabled' });
		}
	}
}

test('renders approved M1–M8 model-first picker states with live-shaped component fixtures', async ({ page, request }) => {
	test.setTimeout(180_000);
	let fixture = { rows: [] as unknown[], accounts: [] as unknown[], policy: undefined as unknown, sources: undefined as unknown[] | undefined };
	await routePicker(page, () => fixture);
	const claudeA = account('claude-primary', 'FOMRRT1', 'claude-code', usage(12, 62));
	const claudeLimited = account('claude-research', 'Research Subscription — Northwest Studio Account', 'claude-code', limited());
	const claudeC = account('claude-team', 'Team Workspace', 'claude-code', usage(43, 94));
	const codexA = account('codex-pro', 'ChatGPT Pro', 'codex', usage(28, 51));
	const codexB = account('codex-lite', 'Personal', 'codex', usage(67, 79));
	const base = (claude: Account[], codex: Account[], claudeEntries = claude.map((item, index) => entry(item, index === 0)), codexEntries = codex.map((item, index) => entry(item, index === 0))) => ({ rows: [...models('claude-code', claude, claudeEntries), ...models('codex', codex, codexEntries), ...external(), ...local()], accounts: [...claude, ...codex], policy: undefined });
	const show = async (id: string, next: typeof fixture, openClaude = false, afterOpen?: () => Promise<void>) => {
		fixture = next;
		const threadBound = ['M2', 'M3', 'M4a', 'M4b', 'M5', 'M7', 'M7-open', 'M9'].includes(id);
		const title = threadBound ? `picker ${id} ${Date.now()}` : undefined;
		if (title) {
			const created = await request.post(authed('/api/threads'), { data: { title, model: 'claude-code/opus' } });
			expect(created.ok()).toBeTruthy();
		}
		await openPicker(page, title);
		if (openClaude) await page.locator('section[aria-label="Claude Code"] button.picker-account-title').click();
		if (id === 'M5') {
			await expect(page.locator('section[aria-label="Claude Code"] .model-list-item.active:not(.blocked)')).toBeVisible();
			expect(await page.locator('section[aria-label="Claude Code"] .picker-account-header').textContent()).toContain('limited · back');
		}
		if (id === 'M7') {
			await expect(page.locator('section[aria-label="Claude Code"] .model-list-item.active.blocked')).toBeVisible();
			expect(await page.locator('section[aria-label="Claude Code"] .picker-account-header').textContent()).toContain('first back');
			await expect(page.locator('section[aria-label="Claude Code"] button.picker-account-title')).toHaveAttribute('aria-expanded', 'false');
			await expect(page.locator('section[aria-label="Claude Code"] .account-usage-row')).toHaveCount(0);
		}
		if (id === 'M9') {
			for (const vendor of ['Claude Code', 'Codex']) await expect(page.locator(`section[aria-label="${vendor}"] button.picker-account-title`)).toHaveAttribute('aria-expanded', 'false');
			await expect(page.locator('.model-sheet .account-usage-row')).toHaveCount(0);
			expect(await page.locator('section[aria-label="Codex"] .picker-account-header').textContent()).toContain('All 2 accounts limited');
		}
		await afterOpen?.();
		await capture(page, id);
	};
	await show('M0-external-local', { rows: [...external(), ...local()], accounts: [], policy: undefined });
	await show('M1', base([claudeA], [codexA]));
	await show('M1-open-reset', base([claudeA], [codexA]), false, async () => {
		await page.locator('section[aria-label="Claude Code"] button.usage').click();
		await expect(page.locator('section[aria-label="Claude Code"] .picker-account-usage div').first()).toBeVisible();
	});
	await show('M2', base([claudeA, claudeLimited, claudeC], [codexA, codexB]));
	await show('M3', base([claudeA, claudeLimited, claudeC], [codexA, codexB]), true);
	const rollover = base([claudeA, claudeLimited, claudeC], [codexA, codexB]);
	rollover.policy = { providers: [{ provider_key: 'claude-code', capabilities: { units: 'accounts', rollover: true }, rollover: true, order: [claudeA.id, claudeLimited.id, claudeC.id], units: [] }] };
	await show('M4a', rollover);
	await show('M4b', rollover, true);
	const limitedA = account('claude-primary', 'FOMRRT1', 'claude-code', limited());
	const claudeRecovery = account('claude-research', 'Research Subscription — Northwest Studio Account', 'claude-code', usage(46, 83));
	const M5 = base([limitedA, claudeRecovery], [codexA, codexB], [entry(limitedA, true, { status: 'unavailable', status_reason: 'usage_limited', resets_at: at }), entry(claudeRecovery)]);
	M5.rows = M5.rows.map((row) => {
		const model = row as { provider?: string };
		return model.provider === 'claude-code' ? { ...model, runnable: { status: 'ready', status_reason: null } } : row;
	});
	await show('M5', M5);
	await show('M5-draft', M5, false, async () => {
		await expect(page.locator('section[aria-label="Claude Code"] .recovery')).toHaveCount(0);
	});
	const spending = account('codex-pro', 'Credits and overage', 'codex', { ...usage(100), allow_overage: true, facts: { credits: { hasCredits: true } } });
	await show('M6', base([claudeA], [spending, codexB]));
	const limitedB = account('claude-research', 'Research Subscription — Northwest Studio Account', 'claude-code', limited());
	const limitedC = account('claude-team', 'Team Workspace', 'claude-code', limited());
	await show('M7', base([limitedA, limitedB, limitedC], [codexA, codexB], [entry(limitedA, true, { status: 'unavailable', status_reason: 'usage_limited', resets_at: at }), entry(limitedB, false, { status: 'unavailable', status_reason: 'usage_limited', resets_at: at }), entry(limitedC, false, { status: 'unavailable', status_reason: 'usage_limited', resets_at: at })]));
	const M7 = base([limitedA, limitedB, limitedC], [codexA, codexB], [entry(limitedA, true, { status: 'unavailable', status_reason: 'usage_limited', resets_at: at }), entry(limitedB, false, { status: 'unavailable', status_reason: 'usage_limited', resets_at: at }), entry(limitedC, false, { status: 'unavailable', status_reason: 'usage_limited', resets_at: at })]);
	await show('M7-open', M7, true, async () => {
		await expect(page.locator('section[aria-label="Claude Code"] .account-usage-row')).toHaveCount(3);
	});
	// Six-account recovery rendering is isolated below so existing M1–M8 evidence retains its own contract.
	// Every vendor collapses alike: a rollover Claude account with a long label beside all-limited Codex.
	const longClaude = account('claude-primary', 'FOMRRT1 · Friends of Montclair Railroad Trail', 'claude-code', usage(1, 60));
	const codexLimitedA = account('codex-pro', 'person@example.test', 'codex', limited());
	const codexLimitedB = account('codex-lite', 'FOMRRT1', 'codex', limited());
	const M9 = base([longClaude, claudeC], [codexLimitedA, codexLimitedB], undefined, [entry(codexLimitedA, true, { status: 'unavailable', status_reason: 'usage_limited', resets_at: at }), entry(codexLimitedB, false, { status: 'unavailable', status_reason: 'usage_limited', resets_at: at })]);
	M9.policy = { providers: [{ provider_key: 'claude-code', capabilities: { units: 'accounts', rollover: true }, rollover: true, order: [longClaude.id, claudeC.id], units: [] }, { provider_key: 'codex', capabilities: { units: 'accounts', rollover: true }, rollover: true, order: [codexLimitedA.id, codexLimitedB.id], units: [] }] };
	await show('M9', M9);
	const localFallback = (source: Record<string, unknown>) => ({ rows: [...external()], accounts: [], policy: undefined, sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup', ...source }] });
	await show('Local-downloading', localFallback({ local_state: 'downloading', download_percent: 40 }), false, async () => {
		const localGroup = page.locator('section[aria-label="CORSIA"]');
		await expect(localGroup.locator('.model-group-head')).toHaveText('CORSIA');
		await expect(localGroup).toContainText('Downloading 40%');
	});
	await show('Local-setup-failed', localFallback({ local_state: 'failed', setup_kind: 'setup' }), false, async () => {
		const localGroup = page.locator('section[aria-label="CORSIA"]');
		await expect(localGroup.locator('.model-group-head')).toHaveText('CORSIA');
		await expect(localGroup).toContainText('Setup failed');
	});
	const signedOut = base([], [codexA, codexB]);
	signedOut.rows = [...models('claude-code', [claudeA], [entry(claudeA, true, { status: 'unavailable', status_reason: 'connection_unavailable' })]), ...models('codex', [codexA, codexB]), ...external(), ...local()];
	await show('M8', signedOut);
});

test('renders six anonymous account recovery states in the actual picker', async ({ page }) => {
	test.setTimeout(90_000);
	let fixture = { rows: [] as unknown[], accounts: [] as unknown[] };
	await routePicker(page, () => fixture);
	const failedKnown = account('claude-known', 'Known reset — unusually long anonymous account label', 'claude-code', { ...limited(), check: { outcome: 'failed', error: 'usage unavailable' } });
	const failedUnknown = account('claude-unknown', 'No reset — unusually long anonymous account label', 'claude-code', { ...limited(), resets_at: null, check: { outcome: 'failed', error: 'usage unavailable' } });
	const checking = account('claude-checking', 'Checking account', 'claude-code', { ...limited(), checking: true });
	const fresh = account('claude-fresh', 'Fresh allowed account', 'claude-code', usage(12, 62));
	const freshCodex = account('codex-fresh', 'Fresh Codex account', 'codex', usage(28, 51));
	const extraCodex = account('codex-extra', 'Extra Codex account', 'codex', usage(67, 79));
	const claudeEntries = [
		entry(failedKnown, true, { status: 'unavailable', status_reason: 'usage_limited', resets_at: at }),
		entry(failedUnknown, false, { status: 'unavailable', status_reason: 'usage_limited', resets_at: null }),
		entry(checking, false, { status: 'unavailable', status_reason: 'usage_limited', resets_at: at }),
		entry(fresh),
	];
	fixture = {
		rows: [...models('claude-code', [failedKnown, failedUnknown, checking, fresh], claudeEntries), ...models('codex', [freshCodex, extraCodex]), ...external(), ...local()],
		accounts: [failedKnown, failedUnknown, checking, fresh, freshCodex, extraCodex],
	};
	await openPicker(page);
	const claude = page.locator('section[aria-label="Claude Code"]');
	const codex = page.locator('section[aria-label="Codex"]');
	const figures = (name: string) => claude.locator('.account-usage-row', { hasText: name }).locator('.figures');
	await claude.locator('button.picker-account-title').click();
	await expect(claude.locator('.account-usage-row')).toHaveCount(4);
	await expect(figures('Known reset — unusually long anonymous account label')).toContainText('back');
	await expect(figures('Known reset — unusually long anonymous account label')).toContainText("couldn't verify");
	await expect(figures('No reset — unusually long anonymous account label')).toContainText("couldn't verify");
	await expect(figures('No reset — unusually long anonymous account label')).not.toContainText('back');
	await expect(figures('Checking account')).toContainText('checking');
	await expect(figures('Fresh allowed account')).toContainText('left');
	await capture(page, 'M604-six-accounts');
	await codex.locator('button.picker-account-title').click();
	await expect(codex.locator('.account-usage-row')).toHaveCount(2);
	await capture(page, 'M604-six-accounts-codex-scrolled', async () => {
		await codex.scrollIntoViewIfNeeded();
		if ((page.viewportSize()?.width ?? 0) <= 480) {
			for (const row of await codex.locator('.account-usage-row .figures').all()) await expect(row).toBeInViewport();
		}
	});
});
