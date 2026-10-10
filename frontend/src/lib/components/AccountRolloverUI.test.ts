import { flushSync, mount, unmount, tick } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import MessageList from './MessageList.svelte';
import InputBarControls from './InputBarControls.svelte';
import { activeThreadId, messages, threads } from '$lib/stores/threads';
import { activeModelId, models, _resetThreadModelTruthForTests } from '$lib/stores/models';
import { coreSettings } from '$lib/stores/settings';
import { token } from '$lib/stores/auth';
import { threadVendorAccounts, loadThreadVendorAccounts } from '$lib/stores/threadVendorAccounts';
import { vendorAccounts, type VendorAccount } from '$lib/stores/vendorAccounts';
import { handleMessage } from '$lib/ws/handlers';
import { _resetTurnStateForTests } from '$lib/ws/turnState';
import { _resetCheckpointsForTests } from '$lib/stores/checkpoints';

let host: HTMLElement;
let components: ReturnType<typeof mount>[];
const id = 'rollover-thread';
const selector = 'exact/saved-selector';
const when = '2026-10-02T20:00:00Z';
function account(provider: string, identity: string, name: string): VendorAccount & { hold_reason: null } {
	return { id: identity, provider, name, is_default: false, state: 'signed_in', revision: 2, identity: {}, label: { name, email: null, text: name }, hold_reason: null };
}
function frame(epoch: number, identity: string) {
	return { type: 'session_updated', thread_id: id, model: selector, updated_at: `2026-10-02T20:00:${String(epoch).padStart(2, '0')}Z`,
		pending_model: null, switch_status: null, switch_error: null, switch_operation_id: null,
		account_id: identity, previous_account_id: 'work', account_switch_reason: 'account_limit', runtime_context_epoch: epoch };
}
function receipt(provider: string, label: string, resets: string | null = '2026-10-03T16:00:00Z', reason = 'account_limit') {
	return { id: `receipt-${provider}`, role: 'system', content: `Moved to ${label} · Personal`, created_at: when,
		seq: 2, delivery_state: 'delivered', delivery_revision: 1, delivery_error: null, active_turn_id: null, write_outcome: null,
		metadata: { type: 'account_switch', reason, provider, model: selector, from_account_id: 'work',
			to_account_id: 'personal', from_account_name: 'Work', to_account_name: 'Personal', resets_at: resets, runtime_context_epoch: 2 } };
}
beforeEach(() => {
	host = document.createElement('div'); document.body.append(host); components = [];
	_resetThreadModelTruthForTests(); _resetTurnStateForTests(); _resetCheckpointsForTests();
	token.set('test-token'); activeThreadId.set(id); activeModelId.set(selector); messages.set([]);
	threads.set([{ id, model: selector, updated_at: when, runtime_context_epoch: 1 } as never]);
	coreSettings.set({ features: { focus_mode: true } } as never);
	threadVendorAccounts.set(new Map()); vendorAccounts.set([]);
	vi.stubGlobal('fetch', vi.fn(async () => Response.json({ accounts: [], cards: [], summaries: [] })));
});
afterEach(async () => {
	for (const component of components) await unmount(component);
	host.remove(); token.set(null); activeThreadId.set(null); activeModelId.set(null); threads.set([]); messages.set([]);
	models.set([]); vendorAccounts.set([]); threadVendorAccounts.set(new Map()); coreSettings.set(null);
	vi.unstubAllGlobals(); vi.restoreAllMocks();
});
function renderList() {
	components.push(mount(MessageList, { target: host, props: { messagesEl: undefined, userIsNearBottom: true,
		ignoreScrollEvents: false, onOpenLightbox: () => {} } })); flushSync();
}
function renderControls() {
	components.push(mount(InputBarControls, { target: host, props: { surface: 'row', sheetView: null,
		toggleSheet: () => {}, isMobile: false, hasImageAttachments: false, inputText: '', pendingAttachmentCount: 0,
		hasMessageToSend: false, canSendToModel: true, queueUntilSwitch: false, onAttachFile: () => {}, onAttachCamera: () => {},
		onAttachPhotos: () => {}, onSendClick: () => {}, onQueueAfterTurn: () => {} } })); flushSync();
}

describe.each([['claude-code', 'Claude Code', 'cc'], ['codex', 'Codex', 'codex']])('%s committed rollover', (provider, label, backend) => {
	const moveLine = () => host.querySelector<HTMLElement>('[data-block-line="account_move"]');
	const openBlock = () => { host.querySelector<HTMLButtonElement>('.fold-toggle')?.click(); flushSync(); };
	it('shows one Focus line inside the block with captured source/destination and authoritative reset details', async () => {
		messages.set([{ id: 'activity', thread_id: id, role: 'assistant', content: 'Internal work', created_at: when }]);
		renderList();
		const payload = { type: 'message_added', thread_id: id, message: receipt(provider, label) };
		handleMessage(payload, () => {}); handleMessage(payload, () => {}); await tick();
		expect(host.querySelectorAll('.transcript-fold')).toHaveLength(1);
		openBlock();
		expect(host.querySelectorAll('[data-block-line="account_move"]')).toHaveLength(1);
		expect(host.querySelector('.account-switch')).toBeNull();
		const line = moveLine();
		expect(line?.closest('.transcript-fold')).not.toBeNull();
		expect(line?.textContent).toContain(`Moved to ${label} · Personal · account limit · Work resets `);
		const text = line?.textContent;
		vendorAccounts.set([account(provider, 'work', 'Renamed Work'), account(provider, 'personal', 'Renamed Personal')]);
		await tick(); expect(moveLine()?.textContent).toBe(text);
		expect(get(activeModelId)).toBe(selector); expect(get(threads)[0].runtime_context_epoch).toBe(1);
	});
	it('shows a not-permitted move as the same line without a limit or reset claim', () => {
		messages.set([{ ...receipt(provider, label, null, 'account_not_permitted'), thread_id: id } as never]); renderList();
		openBlock();
		expect(moveLine()?.textContent).toContain(`Moved to ${label} · Personal`);
		expect(moveLine()?.textContent).not.toContain('account limit'); expect(moveLine()?.textContent).not.toContain('resets');
	});
	it('does not invent a reset when the committed receipt has none', () => {
		messages.set([{ ...receipt(provider, label, null), thread_id: id } as never]); renderList();
		openBlock();
		expect(moveLine()).not.toBeNull();
		expect(moveLine()?.textContent).not.toContain('resets');
	});
	it('never repeats the thread account in the composer, whatever account is bound', async () => {
		models.set([{ id: selector, litellm_model: selector, name: 'Saved Model', provider, backend, status: 'ready', size: '', speed: '' }]);
		vendorAccounts.set([account(provider, 'work', 'Work'), account(provider, 'personal', 'Personal')]);
		threadVendorAccounts.set(new Map([[id, { key: 'read', loading: false, accounts: [account(provider, 'personal', 'Personal')] }]]));
		vi.stubGlobal('fetch', vi.fn(async () => Response.json({ accounts: [account(provider, 'personal', 'Personal')] })));
		renderControls(); await tick();
		expect(host.textContent).toContain('Saved Model');
		expect(host.textContent).not.toContain('Work');
		expect(host.textContent).not.toContain('Personal');
		expect(host.textContent).not.toContain(' · ');
	});
	it('shows the model name alone when a single default account is bound', async () => {
		models.set([{ id: selector, name: 'Saved Model', provider, backend, status: 'ready', size: '', speed: '' }]);
		const work = { ...account(provider, 'work', 'Work'), is_default: true };
		vi.stubGlobal('fetch', vi.fn(async () => Response.json({ accounts: [work] })));
		renderControls(); await tick();
		expect(host.textContent).toContain('Saved Model');
		expect(host.textContent).not.toContain('Work');
		expect(get(activeModelId)).toBe(selector);
	});
	it('updates the durable binding from a confirmed frame while the composer shows no account', async () => {
		models.set([{ id: selector, name: 'Saved Model', provider, backend, status: 'ready', size: '', speed: '' }]);
		threads.set([{ id, model: selector, updated_at: when, runtime_context_epoch: 1, account_id: 'work' } as never]);
		vi.stubGlobal('fetch', vi.fn(async () => Response.json({ accounts: [] })));
		renderControls();
		handleMessage(frame(2, 'personal'), () => {}); await tick();
		expect(get(threads)[0]).toMatchObject({ account_id: 'personal', runtime_context_epoch: 2, model: selector });
		expect(host.textContent).toContain('Saved Model');
		expect(host.textContent).not.toContain('Work');
		expect(host.textContent).not.toContain('Personal');
	});
});

it('uses the latest current GET even when an earlier account read finishes last', async () => {
	let old!: (value: Response) => void;
	vi.stubGlobal('fetch', vi.fn().mockImplementationOnce(() => new Promise<Response>((resolve) => { old = resolve; }))
		.mockResolvedValueOnce(Response.json({ accounts: [account('codex', 'new', 'New')] })));
	const before = loadThreadVendorAccounts(id, 'epoch-1');
	await loadThreadVendorAccounts(id, 'epoch-2'); old(Response.json({ accounts: [account('codex', 'old', 'Old')] })); await before;
	expect(get(threadVendorAccounts).get(id)?.accounts[0].id).toBe('new');
});

it('does not replace a newer snapshot binding with an equal-epoch conflicting event', () => {
	threads.set([{ id, model: selector, updated_at: when, account_id: 'new', runtime_context_epoch: 7 } as never]);
	handleMessage(frame(7, 'old'), () => {});
	expect(get(threads)[0].account_id).toBe('new');
});
