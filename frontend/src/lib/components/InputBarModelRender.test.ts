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
import InputBarControls from './InputBarControls.svelte';
import rowSource from './v7/AccountUsageRow.svelte?raw';
import headerSource from './PickerAccountHeader.svelte?raw';
import sheetSource from './InputBarModelSheet.svelte?raw';
import actionRowSource from './InputBarActionRow.svelte?raw';
import { composerCanSend } from './composerSendReadiness';
import { token } from '$lib/stores/auth';
import { pickerPolicy } from '$lib/stores/accountPicker';
import { activeModelId, models, type Model } from '$lib/stores/models';
import type { RunsOn } from '$lib/stores/modelTurn';
import { activeThreadId, threads } from '$lib/stores/threads';
import { _resetTurnStateForTests, applyLifecycle } from '$lib/ws/turnState';
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
const open = (showAccounts = true) => {
	component = mount(InputBarModelSheet, { target: host });
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
	models.set([]);
	threads.set([]);
	activeThreadId.set(null);
	activeModelId.set(null);
	token.set(null);
});


const LIMITED_ON_WORK = [
	entry('work', { selected: true, status: 'unavailable', status_reason: 'usage_limited', resets_at: '2099-01-01T00:00:00Z', usage: { state: 'limited', usable: false, windows: [] } }),
	entry('personal'),
];
const READY: Model['runnable'] = { status: 'ready', status_reason: null };
const BLOCKED_BY_LIMIT: Model['runnable'] = { status: 'unavailable', status_reason: 'usage_limited' };
const catalog = (runnable?: Model['runnable']) => [
	model('opus', 'Opus', LIMITED_ON_WORK, { runnable }),
	model('sonnet', 'Sonnet', LIMITED_ON_WORK, { runnable }),
];
const items = () => [...host.querySelectorAll<HTMLButtonElement>('.model-list-item')];
const chip = () => host.querySelector<HTMLElement>('.model-chip');
const mountControls = () => {
	component = mount(InputBarControls, {
		target: host,
		props: {
			surface: 'row', sheetView: null, toggleSheet: () => {}, isMobile: false, hasImageAttachments: false,
			inputText: '', pendingAttachmentCount: 0, hasMessageToSend: false, canSendToModel: true, queueUntilSwitch: false,
			onAttachFile: () => {}, onAttachCamera: () => {}, onAttachPhotos: () => {}, onSendClick: () => {}, onQueueAfterTurn: () => {},
		},
	});
	flushSync();
};

describe('a thread bound to a limited account while another can run the model (P5)', () => {
	beforeEach(() => {
		vendorAccounts.set([
			account('work', 'Work', {}, { state: 'limited', usable: false, resets_at: '2099-01-01T00:00:00Z' }, 'work@example.test'),
			account('personal', 'Personal'),
		]);
	});

	it('keeps the models selectable and the account row limited when the server says the model can run', () => {
		models.set(catalog(READY));
		open();
		expect(host.querySelector('[data-state="ready"]')).not.toBeNull();
		expect(items().map((item) => item.disabled)).toEqual([false, false]);
		expect(items().some((item) => item.classList.contains('blocked'))).toBe(false);
		expect(items().some((item) => item.textContent?.includes('limited'))).toBe(false);
		expect(rowText(accountRows()[0])).toContain('limited');
		expect(accountRows()[0].querySelector('.mark')).toBeNull();
	});

	it('blocks every model, still, when no account can run it (P7)', () => {
		models.set(catalog(BLOCKED_BY_LIMIT));
		open();
		expect(items().map((item) => item.disabled)).toEqual([true, true]);
		expect(items().every((item) => item.classList.contains('blocked'))).toBe(true);
	});

	it('keeps a model not offered on the marked account blocked, with the others selectable (P4)', () => {
		const opus = model('opus', 'Opus', [entry('work', { selected: true, status: 'unavailable', status_reason: 'account_model_not_discovered' }), entry('personal')], {
			runnable: { status: 'unavailable', status_reason: 'account_model_not_discovered' },
		});
		models.set([opus, model('sonnet', 'Sonnet', ON_WORK), model('haiku', 'Haiku', ON_WORK)]);
		vendorAccounts.set([account('work', 'Work', {}, {}, 'work@example.test'), account('personal', 'Personal')]);
		activeModelId.set('claude-code/sonnet');
		open();
		expect(rowText(host.querySelector('.picker-account-header')!)).toContain('Claude Code · Work');
		expect(items().map((item) => item.disabled)).toEqual([true, false, false]);
		expect(items()[0].textContent).toContain('not offered on Work');
	});

	it('leaves the composer chip normal, as the held line explains the limit (P5)', () => {
		models.set(catalog(READY));
		mountControls();
		expect(chip()?.textContent).toContain('Opus');
		expect(chip()?.textContent).not.toContain('Unavailable');
		expect(chip()?.classList.contains('unloaded')).toBe(false);
	});

	it('leaves the composer chip normal when every account is limited too (P7)', () => {
		models.set(catalog(BLOCKED_BY_LIMIT));
		mountControls();
		expect(chip()?.textContent).toContain('Opus');
		expect(chip()?.textContent).not.toContain('Unavailable');
		expect(chip()?.classList.contains('unloaded')).toBe(false);
	});

	it('keeps the spending-credits warning out of the composer chip and shows it on the account in the picker', () => {
		activeModelId.set('codex/opus');
		threads.set([{ id: 'thread-1', model: 'codex/opus', updated_at: '2026-10-05T10:00:00Z', account_id: 'work', runtime_context_epoch: 7 } as never]);
		models.set([model('opus', 'Opus 5.5', [entry('work', { selected: true }), entry('personal')], {
			connection: 'codex', provider: 'codex', litellm_model: 'codex/opus',
		})]);
		vendorAccounts.set([
			account('work', 'ChatGPT Pro', { provider: 'codex' }, {
				allow_overage: true,
				windows: [{ ...WEEK, used_percent: 100 }],
				facts: { credits: { hasCredits: true } },
			}),
			account('personal', 'Personal', { provider: 'codex' }),
		]);
		mountControls();
		expect(rowText(chip()!)).toBe('Opus 5.5');
		unmount(component!);
		component = null;
		open(false);
		expect(host.querySelector('.picker-account-header')?.textContent).toContain('plan used · spending credits');
		expect(accountRows()).toHaveLength(0);
	});

	it('still says Unavailable for a block that is not a usage limit (not offered, signed out, model gone)', () => {
		const blocked = (reason: string) => [model('opus', 'Opus', [entry('work', { selected: true, status: 'unavailable', status_reason: reason }), entry('personal')], {
			runnable: { status: 'unavailable', status_reason: reason },
		})];
		for (const reason of ['account_model_not_discovered', 'connection_unavailable']) {
			models.set(blocked(reason));
			mountControls();
			expect(chip()?.textContent).toContain('Unavailable · Opus');
			expect(chip()?.classList.contains('unloaded')).toBe(true);
			unmount(component!);
			component = null;
			host.innerHTML = '';
		}
		models.set([]);
		mountControls();
		expect(chip()?.textContent).toContain('Unavailable · claude-code/opus');
	});

	it('lets the composer send into a usage limit, for the server to hold, including when every account is limited (P5, P7)', () => {
		const thread = { model: 'claude-code/opus' } as never;
		const send = (rows: Model[]) => composerCanSend(thread, rows[0], rows);
		expect(send(catalog(READY))).toBe(true);
		expect(send(catalog(BLOCKED_BY_LIMIT))).toBe(true);
		expect(send(catalog())).toBe(true);
	});

	it('keeps the send disabled for any other block: not offered, signed out, model gone', () => {
		const thread = { model: 'claude-code/opus' } as never;
		for (const reason of ['account_model_not_discovered', 'connection_unavailable', 'account_project_denied']) {
			const rows = [model('opus', 'Opus', LIMITED_ON_WORK, { runnable: { status: 'unavailable', status_reason: reason } })];
			expect(composerCanSend(thread, rows[0], rows)).toBe(false);
		}
		expect(composerCanSend(thread, undefined, [])).toBe(false);
	});
});

describe('account rows while a turn is running', () => {
	afterEach(() => _resetTurnStateForTests());

	it('takes a choice during the turn and sends it, for the next turn, as a model choice is', async () => {
		pinReply = () => json(200, { account_id: 'personal', runtime_context_epoch: 7, changed: false, pending: true });
		open();
		applyLifecycle('thread-1', { turnId: 'turn-1', turnOrder: 1 });
		flushSync();
		const [, personal] = accountRows() as HTMLButtonElement[];
		expect(personal.disabled).toBe(false);
		expect(personal.title).toBe('');
		personal.click();
		await settle();
		expect(puts()).toHaveLength(1);
		expect(host.textContent).not.toContain('try again when it finishes');
		// The running turn keeps its account until the next turn moves.
		expect(get(threads)[0]).toMatchObject({ account_id: 'work' });
	});

	it('marks the held choice with its accessible switching icon until the next turn moves to it', () => {
		const held = [entry('work'), entry('personal', { selected: true, pending: true })];
		models.set([model('opus', 'Opus', held), model('sonnet', 'Sonnet', held)]);
		open();
		const [work, personal] = accountRows();
		expect(personal.classList.contains('marked')).toBe(true);
		expect(personal.querySelector('[aria-label="Switching account"]')).not.toBeNull();
		expect(personal.querySelector('.switching-slot')).not.toBeNull();
		// It follows the name of the account being switched to, not the usage figures at the row's end.
		expect(personal.querySelector('.name + .switching-slot')).not.toBeNull();
		expect(work.querySelector('[aria-label="Switching account"]')).toBeNull();
		models.set(CATALOG);
		flushSync();
		expect(host.querySelector('.account-usage-row.picker [aria-label="Switching account"]')).toBeNull();
	});

	it('shows the account icon in the collapsed header, then only on its expanded row', () => {
		const held = [entry('work'), entry('personal', { selected: true, pending: true })];
		models.set([model('opus', 'Opus', held), model('sonnet', 'Sonnet', held)]);
		open(false);
		expect(host.querySelector('.picker-account-header [aria-label="Switching account"]')).not.toBeNull();
		expect(host.querySelector('.picker-account-header .switching-slot')).not.toBeNull();
		host.querySelector<HTMLButtonElement>('button.picker-account-title')?.click();
		flushSync();
		expect(host.querySelector('.picker-account-header [aria-label="Switching account"]')).toBeNull();
		expect(host.querySelector('.account-usage-row.picker [aria-label="Switching account"]')).not.toBeNull();
	});

	it('names the pending model icon, then keeps the failed-switch words after it fails', () => {
		threads.set([{ id: 'thread-1', model: 'claude-code/opus', pending_model: 'claude-code/sonnet', switch_status: 'pending', updated_at: '2026-10-05T10:00:00Z', account_id: 'work', runtime_context_epoch: 7 } as never]);
		open(false);
		const sonnet = items().find((item) => item.textContent?.includes('Sonnet'))!;
		expect(sonnet.querySelector('[aria-label="Switching model"]')).not.toBeNull();
		threads.set([{ id: 'thread-1', model: 'claude-code/opus', pending_model: 'claude-code/sonnet', switch_status: 'failed', switch_error: 'Model unavailable', updated_at: '2026-10-05T10:00:00Z', account_id: 'work', runtime_context_epoch: 7 } as never]);
		flushSync();
		expect(sonnet.textContent).toContain('Switch failed');
		expect(sonnet.querySelector('[aria-label="Switching model"]')).toBeNull();
	});
});

describe('composer switching indicators', () => {
	it('names the account switch and removes its icon when the pending account clears', () => {
		const held = [entry('work', { selected: true, pending: true }), entry('personal')];
		models.set([model('opus', 'Opus', held)]);
		mountControls();
		expect(chip()?.querySelector('[aria-label="Switching account"]')).not.toBeNull();
		expect(chip()?.getAttribute('title')).toBe('Account switch in progress');
		expect(chip()?.querySelector('.model-switch-slot')).not.toBeNull();
		models.set(CATALOG);
		flushSync();
		expect(chip()?.querySelector('[aria-label="Switching account"]')).toBeNull();
	});

	it('names a pending model switch and keeps a failed switch as a red status dot', () => {
		mountControls();
		threads.set([{ id: 'thread-1', model: 'claude-code/opus', pending_model: 'claude-code/sonnet', switch_status: 'pending', updated_at: '2026-10-05T10:00:00Z', account_id: 'work', runtime_context_epoch: 7 } as never]);
		flushSync();
		expect(chip()?.querySelector('[aria-label="Switching model"]')).not.toBeNull();
		expect(chip()?.getAttribute('title')).toBe('Model switch in progress');
		threads.set([{ id: 'thread-1', model: 'claude-code/opus', pending_model: 'claude-code/sonnet', switch_status: 'failed', switch_error: 'Model unavailable', updated_at: '2026-10-05T10:00:00Z', account_id: 'work', runtime_context_epoch: 7 } as never]);
		flushSync();
		expect(chip()?.querySelector('[aria-label="Switching model"]')).toBeNull();
		expect(chip()?.querySelector('[aria-label="Switch failed"]')).not.toBeNull();
		expect(chip()?.getAttribute('title')).toBe('Model unavailable');
	});
});

describe('switching indicator motion', () => {
	it('puts every switching icon in an inlineSlot wrapper', () => {
		for (const source of [rowSource, headerSource, sheetSource, actionRowSource]) {
			expect(source).toMatch(/in:inlineSlot out:inlineSlot/);
		}
		expect(actionRowSource).toContain('model-switch-slot');
	});
});

describe('account row selected fill', () => {
	it('keeps the current row fill under hover, as the model row does', () => {
		expect(rowSource).toMatch(/picker:hover:not\(:disabled\):not\(\.marked\)/);
	});
});

describe('account label separator', () => {
	it('sets the middle dot off from the name and the email with the mockup spacing', () => {
		expect(rowSource).toMatch(/\.picker-label \.sep \{[^}]*padding: 0 3px/);
	});
});
