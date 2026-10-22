import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));
vi.mock('$lib/stores/modelsLoad', () => ({ loadModels: vi.fn(async () => {}), loadComposerModels: vi.fn(async () => {}), loadThreadCatalog: vi.fn(async () => {}) }));
vi.mock('$lib/stores/threadVendorAccounts', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/threadVendorAccounts')>()),
	loadThreadVendorAccounts: vi.fn(async () => {}),
}));

import InputBarModelSheet from './InputBarModelSheet.svelte';
import sheetSource from './InputBarModelSheet.svelte?raw';
import { token } from '$lib/stores/auth';
import { pickerPolicy, pickerPolicyProject } from '$lib/stores/accountPicker';
import { activeModelId, models, type Model } from '$lib/stores/models';
import type { RunsOn } from '$lib/stores/modelTurn';
import { activeThreadId, threads } from '$lib/stores/threads';
import { threadSettingsOpen } from '$lib/stores/threadSettings';
import { providerCardRequest, resetSettingsNavigation, settingsNavigation } from '$lib/stores/settingsNavigation';
import { vendorAccounts, type VendorAccount, type VendorAccountUsage } from '$lib/stores/vendorAccounts';

const CAPS = {
	context_window_max: 200000, supports_vision: true, supports_tools: true,
	supports_reasoning: true, reasoning_config: { type: 'boolean' as const, default: true },
};

const WEEK = { kind: 'seven_day', state: 'reported' as const, used_percent: 71, window_minutes: 10080, resets_at: '2099-01-01T00:00:00Z' };
const LIMITED: Partial<VendorAccountUsage> = { state: 'limited', usable: false, resets_at: '2099-01-01T00:00:00Z' };

function account(id: string, name: string, over: Partial<VendorAccount> = {}, usage: Partial<VendorAccountUsage> = {}, email: string | null = null): VendorAccount {
	return {
		id, provider: 'claude-code', name, is_default: false, state: 'signed_in', revision: 1, identity: {},
		label: { name, email, text: name, custom: null },
		usage: { state: 'allowed', usable: true, windows: [WEEK], checking: false, check: null, ...usage },
		...over,
	};
}

function entry(accountId: string, over: Partial<RunsOn> = {}): RunsOn {
	return {
		account_id: accountId, selected: false, status: 'ready', status_reason: null, resets_at: null,
		usage: { state: 'allowed', usable: true, windows: [] }, ...over,
	};
}

function model(id: string, name: string, runsOn: RunsOn[], over: Partial<Model> = {}): Model {
	return {
		id, name, display_name: name, size: '', backend: 'cc', provider: 'claude-code', connection: 'claude-code', speed: '',
		status: 'ready', available: true, litellm_model: `claude-code/${id}`, capabilities: CAPS, runs_on: runsOn, ...over,
	} as Model;
}

const ON_WORK = [entry('work', { selected: true }), entry('personal')];
const CATALOG = [model('opus', 'Opus', ON_WORK), model('sonnet', 'Sonnet', ON_WORK)];

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const originalFetch = globalThis.fetch;
const fetchMock = vi.fn();
let pinReply: () => Response;

const json = (status: number, body: unknown) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const open = (showAccounts = true, onclose?: () => void) => {
	component = mount(InputBarModelSheet, { target: host, props: { onclose } });
	flushSync();
	if (showAccounts && accountRows().length === 0) host.querySelector<HTMLButtonElement>('button.picker-account-title')?.click();
	flushSync();
};
const accountRows = () => [...host.querySelectorAll<HTMLElement>('.account-usage-row.picker')];
const rowText = (el: Element) => (el.textContent ?? '').replace(/\s+/g, ' ').trim();
const puts = () => fetchMock.mock.calls.filter(([, init]) => init?.method === 'PUT');
const settle = async () => { await new Promise((resolve) => setTimeout(resolve, 20)); flushSync(); };

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('synthetic');
	activeThreadId.set('thread-1');
	threads.set([{ id: 'thread-1', model: 'claude-code/opus', updated_at: '2026-10-05T10:00:00Z', account_id: 'work', runtime_context_epoch: 7 } as never]);
	activeModelId.set('claude-code/opus');
	models.set(CATALOG);
	pickerPolicy.set({});
	pickerPolicyProject.set(undefined);
	providerCardRequest.set(null);
	resetSettingsNavigation();
	threadSettingsOpen.set(false);
	vendorAccounts.set([account('work', 'Work', {}, {}, 'work@example.test'), account('personal', 'Personal')]);
	pinReply = () => json(200, { account_id: 'personal', runtime_context_epoch: 8, changed: true });
	fetchMock.mockReset();
	fetchMock.mockImplementation(async (url: string, init?: RequestInit) => {
		if (init?.method === 'PUT') return pinReply();
		if (String(url).includes('/api/vendor-accounts')) return json(200, { accounts: get(vendorAccounts) });
		return json(200, {});
	});
	globalThis.fetch = fetchMock as unknown as typeof fetch;
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	globalThis.fetch = originalFetch;
	vendorAccounts.set([]);
	pickerPolicy.set({});
	pickerPolicyProject.set(undefined);
	providerCardRequest.set(null);
	resetSettingsNavigation();
	threadSettingsOpen.set(false);
	models.set([]);
	threads.set([]);
	activeThreadId.set(null);
	activeModelId.set(null);
	token.set(null);
});

describe('model picker account rows', () => {
	it('lists every account of a vendor with several, with its own figures and a marked row without a second check', () => {
		open();
		const rows = accountRows();
		expect(rows.map((row) => row.querySelector('.name')?.textContent)).toEqual(['Work', 'Personal']);
		expect(rowText(rows[0])).toContain('work@example.test');
		expect(rows.map((row) => row.querySelector('.figures')?.textContent?.trim())).toEqual(['wk 29% left', 'wk 29% left']);
		expect(rows.map((row) => row.querySelector('.mark') !== null)).toEqual([false, false]);
		expect(rows.map((row) => (row as HTMLButtonElement).getAttribute('aria-pressed'))).toEqual(['true', 'false']);
		// The header and the list read one value: the account the header names carries the model rows' check.
		const marked = rows.filter((row) => row.classList.contains('marked'));
		expect(marked).toHaveLength(1);
		expect(host.querySelector('.picker-account-header .account-name')?.textContent).toBe(marked[0].querySelector('.name')?.textContent);
		expect(rows.map((row) => row.querySelector('.check-glyph')?.getAttribute('aria-label') ?? null)).toEqual(['Current', null]);
	});

	it('reads the accounts and the thread\'s project policy when the picker opens', () => {
		threads.set([{ id: 'thread-1', model: 'claude-code/opus', updated_at: '2026-10-05T10:00:00Z', project_id: 'project-1', account_id: 'work', runtime_context_epoch: 7 } as never]);
		open();
		const urls = fetchMock.mock.calls.map(([url]) => String(url));
		expect(urls.some((url) => url.includes('/api/vendor-accounts'))).toBe(true);
		expect(urls).toContain('/api/provider-policy?project_id=project-1');
	});

	it('keeps a single account as a non-disclosure header with its usage', () => {
		vendorAccounts.set([account('work', 'Work')]);
		open(false);
		expect(accountRows()).toHaveLength(0);
		expect(host.querySelector('.picker-account-header')?.textContent).toContain('Claude Code · Work');
		expect(host.querySelector('.picker-account-header')?.textContent).toContain('wk 29% left');
		expect(host.querySelector('button.picker-account-title')).toBeNull();
	});

	it('starts several accounts collapsed and reveals every row only from the vendor header', () => {
		open(false);
		const header = host.querySelector<HTMLButtonElement>('button.picker-account-title')!;
		expect(header.getAttribute('aria-expanded')).toBe('false');
		expect(accountRows()).toHaveLength(0);
		header.click();
		flushSync();
		expect(header.getAttribute('aria-expanded')).toBe('true');
		expect(accountRows()).toHaveLength(2);
	});

	it('keeps the selected rollover account in the header, and marks that same account current in the ordered list', () => {
		open();
		expect(host.querySelectorAll('.account-usage-row .order')).toHaveLength(0);
		unmount(component!);
		component = null;
		pickerPolicy.set({ 'claude-code': { rollover: true, order: ['work', 'personal'] } });
		pickerPolicyProject.set(null);
		open();
		expect(host.querySelector('.picker-account-header')?.textContent).toContain('Auto · Work');
		expect(accountRows().map((row) => [row.querySelector('.name')?.textContent, row.querySelector('.order')?.textContent])).toEqual([['Work', '1'], ['Personal', '2']]);
		expect(accountRows().map((row) => row.classList.contains('marked'))).toEqual([true, false]);
		expect(accountRows().map((row) => row.querySelector('.check-glyph') !== null)).toEqual([true, false]);
	});

	it('opens Settings at the workspace account order from the rollover footer and closes the picker', () => {
		pickerPolicy.set({ 'claude-code': { rollover: true, order: ['work', 'personal'] } });
		pickerPolicyProject.set(null);
		threadSettingsOpen.set(true);
		const onclose = vi.fn();
		open(false, onclose);
		host.querySelector<HTMLButtonElement>('button.picker-account-title')!.click();
		flushSync();
		host.querySelector<HTMLButtonElement>('.rollover-settings button')!.click();
		expect(get(threadSettingsOpen)).toBe(false);
		expect(get(settingsNavigation).openRequest).toBe(1);
		expect(get(settingsNavigation).section).toBe('providers');
		expect(get(settingsNavigation).project).toBeNull();
		expect(get(settingsNavigation).anchor).toBeNull();
		expect(onclose).toHaveBeenCalledOnce();
	});

	it('opens the project\'s Settings page at its account order when the thread belongs to a project', () => {
		threads.set([{ id: 'thread-1', model: 'claude-code/opus', updated_at: '2026-10-05T10:00:00Z', project_id: 'project-1', account_id: 'work', runtime_context_epoch: 7 } as never]);
		pickerPolicy.set({ 'claude-code': { rollover: true, order: ['work', 'personal'] } });
		pickerPolicyProject.set('project-1');
		open(false);
		host.querySelector<HTMLButtonElement>('button.picker-account-title')!.click();
		flushSync();
		host.querySelector<HTMLButtonElement>('.rollover-settings button')!.click();
		expect(get(threadSettingsOpen)).toBe(false);
		expect(get(settingsNavigation).section).toBe('projects');
		expect(get(settingsNavigation).project).toBe('project-1');
		expect(get(settingsNavigation).anchor).toBe('project-provider-policy');
	});

	it('shows no order from another project\'s policy, and the thread\'s own once its read lands', async () => {
		threads.set([{ id: 'thread-1', model: 'claude-code/opus', updated_at: '2026-10-05T10:00:00Z', project_id: 'project-1', account_id: 'work', runtime_context_epoch: 7 } as never]);
		pickerPolicy.set({ 'claude-code': { rollover: true, order: ['personal', 'work'] } });
		pickerPolicyProject.set('project-x');
		fetchMock.mockImplementation(async (url: string) => String(url).startsWith('/api/provider-policy')
			? json(200, { providers: [{
				provider_key: 'claude-code', capabilities: { units: 'accounts', rollover: true },
				rollover: true, order: ['work', 'personal'], units: [],
			}] })
			: String(url).includes('/api/vendor-accounts') ? json(200, { accounts: get(vendorAccounts) }) : json(200, {}));
		open();
		expect(host.querySelectorAll('.account-usage-row .order')).toHaveLength(0);
		await settle();
		expect(accountRows().map((row) => [row.querySelector('.name')?.textContent, row.querySelector('.order')?.textContent])).toEqual([['Work', '1'], ['Personal', '2']]);
		expect(accountRows().map((row) => row.classList.contains('marked'))).toEqual([true, false]);
		expect(accountRows().map((row) => row.querySelector('.check-glyph') !== null)).toEqual([true, false]);
	});

	it('sends exactly one pin carrying only the account when a row is chosen', async () => {
		open();
		accountRows()[1].click();
		await settle();
		expect(puts()).toHaveLength(1);
		const [url, init] = puts()[0];
		expect(String(url)).toMatch(/^\/api\/threads\/thread-1\/vendor-account\?token=/);
		expect(JSON.parse(init.body)).toEqual({ account_id: 'personal' });
	});

	it('sends nothing for the account already marked', async () => {
		open();
		accountRows()[0].click();
		await settle();
		expect(puts()).toHaveLength(0);
	});

	it('shows a limited account blocked and sends nothing when it is tapped', async () => {
		vendorAccounts.set([account('work', 'Work'), account('personal', 'Personal', {}, LIMITED)]);
		open();
		const blocked = accountRows()[1] as HTMLButtonElement;
		expect(blocked.disabled).toBe(true);
		expect(blocked.classList.contains('blocked')).toBe(true);
		expect(rowText(blocked)).toContain('limited · back');
		blocked.click();
		await settle();
		expect(puts()).toHaveLength(0);
	});

	it('shows a signed-out account blocked in words', async () => {
		vendorAccounts.set([account('work', 'Work'), account('personal', 'Personal', { state: 'signed_out' })]);
		open();
		const blocked = accountRows()[1] as HTMLButtonElement;
		expect(blocked.disabled).toBe(true);
		expect(rowText(blocked)).toContain('signed out');
	});

	it('opens a vendor whose every model is limited, so a person can reach the account that is not', () => {
		models.set([
			model('opus', 'Opus', [entry('work', { selected: true, status: 'unavailable', status_reason: 'usage_limited' }), entry('personal')], { status: 'unavailable' }),
		]);
		vendorAccounts.set([account('work', 'Work', {}, LIMITED), account('personal', 'Personal')]);
		open();
		expect(host.querySelector('[data-state="limited"] button.picker-account-title')?.getAttribute('aria-expanded')).toBe('true');
		expect(host.querySelector('.picker-account-header')?.textContent).toContain('limited · back');
		expect(host.querySelector<HTMLButtonElement>('.recovery')?.textContent).toBe('Use Personal');
		const rows = accountRows() as HTMLButtonElement[];
		expect(rows.map((row) => row.disabled)).toEqual([true, false]);
	});

	it('says why a turn could not move, in one sentence, and keeps the choice open', async () => {
		pinReply = () => json(409, { detail: 'account_change_requires_idle; wait' });
		open();
		accountRows()[1].click();
		await settle();
		expect(host.querySelector('[role="alert"]')?.textContent).toBe('A turn is running; try again when it finishes.');
		expect(accountRows()[0].querySelector('.mark')).toBeNull();
	});

	it('says the generic sentence for any other refusal', async () => {
		pinReply = () => json(500, { detail: 'boom' });
		open();
		accountRows()[1].click();
		await settle();
		expect(host.querySelector('[role="alert"]')?.textContent).toBe("Couldn't switch accounts. Try again.");
	});

	it('clears an earlier refusal when the next choice goes through', async () => {
		pinReply = () => json(409, { detail: 'account_change_requires_idle; wait' });
		open();
		accountRows()[1].click();
		await settle();
		expect(host.querySelector('[role="alert"]')).not.toBeNull();
		pinReply = () => json(200, { account_id: 'personal', runtime_context_epoch: 8, changed: true });
		accountRows()[1].click();
		await settle();
		expect(host.querySelector('[role="alert"]')).toBeNull();
	});

	it('does not offer a recovery account in a draft chat', () => {
		models.set([
			model('opus', 'Opus', [entry('work', { selected: true, status: 'unavailable', status_reason: 'usage_limited' }), entry('personal')], { status: 'unavailable' }),
		]);
		vendorAccounts.set([account('work', 'Work', {}, LIMITED), account('personal', 'Personal')]);
		activeThreadId.set(null);
		open();
		expect(host.querySelector('.recovery')).toBeNull();
	});

	it('shows the rows read-only in a chat with no thread yet, and sends nothing', async () => {
		activeThreadId.set(null);
		open();
		const rows = accountRows();
		expect(rows).toHaveLength(2);
		expect(host.querySelectorAll('.account-usage-row.picker button, button.account-usage-row.picker')).toHaveLength(0);
		rows[1].click();
		await settle();
		expect(puts()).toHaveLength(0);
	});

	it('notes a model the marked account does not offer, naming that account', () => {
		models.set([
			model('opus', 'Opus', ON_WORK),
			model('haiku', 'Haiku', [entry('work', { selected: true, status: 'unavailable', status_reason: 'account_model_not_discovered' }), entry('personal')], { status: 'unavailable', status_reason: 'account_model_not_discovered' }),
		]);
		open();
		const notes = [...host.querySelectorAll('.row-note')].map((note) => note.textContent);
		expect(notes).toEqual(['not offered on Work']);
	});

	it('keeps a blocked current model at full strength, not dimmed', () => {
		models.set([
			model('opus', 'Opus', [entry('work', { selected: true, status: 'unavailable', status_reason: 'usage_limited' }), entry('personal')], { status: 'unavailable' }),
			model('sonnet', 'Sonnet', ON_WORK),
		]);
		open();
		const current = host.querySelector<HTMLButtonElement>('.model-list-item.blocked.active')!;
		expect(current).not.toBeNull();
		expect(current.disabled).toBe(true);
		// The current row is not dimmed: no rule gives a blocked, active row its own opacity.
		expect(sheetSource).not.toMatch(/\.model-list-item\.blocked\.active\s*\{[^}]*opacity/);
	});

	it('keeps every-account-limited collapsed to one header, opening the accounts on tap', async () => {
		models.set([model('opus', 'Opus', [
			entry('work', { selected: true, status: 'unavailable', status_reason: 'usage_limited' }),
			entry('personal', { status: 'unavailable', status_reason: 'usage_limited' }),
		], { status: 'unavailable' })]);
		vendorAccounts.set([account('work', 'Work', {}, LIMITED), account('personal', 'Personal', {}, LIMITED)]);
		open(false);
		expect(host.querySelector('.picker-account-header')?.textContent).toContain('All 2 accounts limited');
		expect(accountRows()).toHaveLength(0);
		expect(host.querySelectorAll('.model-list-item')).toHaveLength(1);
		const title = host.querySelector<HTMLButtonElement>('button.picker-account-title')!;
		expect(title.getAttribute('aria-expanded')).toBe('false');
		title.click();
		flushSync();
		expect(accountRows()).toHaveLength(2);
	});

	it('shows a signed-out account vendor and keeps its models visible', () => {
		models.set([model('opus', 'Opus', [entry('work', { selected: true, status: 'unavailable', status_reason: 'connection_unavailable' })], { status: 'unavailable' })]);
		vendorAccounts.set([]);
		open(false);
		expect(host.querySelector('.picker-account-header')?.textContent).toContain('signed out');
		expect(host.querySelector<HTMLButtonElement>('.recovery')?.textContent).toBe('Sign in');
		expect(host.querySelectorAll('.model-list-item')).toHaveLength(1);
		expect(host.querySelector('.model-group')?.classList.contains('signed-out')).toBe(true);
	});
});
