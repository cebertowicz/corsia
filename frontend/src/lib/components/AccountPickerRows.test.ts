import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

const reloadAccounts = vi.hoisted(() => vi.fn(async () => {}));
const reloadModels = vi.hoisted(() => vi.fn(async () => {}));
const pin = vi.hoisted(() => vi.fn(async () => ({ ok: true, changed: true })));
vi.mock('$lib/stores/threadVendorAccounts', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/threadVendorAccounts')>()),
	loadThreadVendorAccounts: reloadAccounts,
}));
vi.mock('$lib/stores/modelsLoad', () => ({ loadModels: reloadModels, loadComposerModels: reloadModels }));
vi.mock('$lib/stores/threadAccountPin', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/threadAccountPin')>()),
	pinThreadAccount: pin,
}));

import AccountPickerRows from './AccountPickerRows.svelte';
import { token } from '$lib/stores/auth';
import { activeModelId } from '$lib/stores/models';
import { activeThreadId } from '$lib/stores/threads';
import { corsiaAgentId } from '$lib/utils/conductor';
import type { PickerAccount } from '$lib/stores/accountPicker';

const ROW = {
	account: { id: 'acc-2', provider: 'claude-code', name: 'Personal', is_default: false, state: 'signed_in', revision: 1, identity: {}, label: { name: 'Personal', email: 'me@example.com', text: 'me@example.com', custom: null } },
	marked: false, pending: false, order: null, choosable: true,
} as unknown as PickerAccount;

const fetchMock = vi.fn();
let host: HTMLElement;
let component: Record<string, unknown> | null = null;

async function choose() {
	component = mount(AccountPickerRows, { target: host, props: { rows: [ROW] } });
	flushSync();
	host.querySelector<HTMLButtonElement>('button.account-usage-row')!.click();
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('tok');
	fetchMock.mockReset();
	pin.mockClear();
	reloadAccounts.mockClear();
	reloadModels.mockClear();
	vi.stubGlobal('fetch', fetchMock);
	corsiaAgentId.set('cond-1');
	activeThreadId.set('cond-1');
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.unstubAllGlobals();
	token.set(null);
	corsiaAgentId.set(null);
	activeThreadId.set(null);
	activeModelId.set(null);
});

describe('AccountPickerRows on the model-less conductor', () => {
	it('binds through the corsia-agent-account step, not the pin route, and reads the binding and catalog again', async () => {
		activeModelId.set(null);
		fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ bound: true, reason: null }), { status: 200 }));
		await choose();
		await vi.waitFor(() => expect(reloadModels).toHaveBeenCalled());
		expect(fetchMock.mock.calls[0][0]).toBe('/api/threads/cond-1/corsia-agent-account');
		expect(JSON.parse((fetchMock.mock.calls[0][1] as RequestInit).body as string)).toEqual({ account_id: 'acc-2' });
		expect(pin).not.toHaveBeenCalled();
		expect(reloadAccounts).toHaveBeenCalled();
	});

	it('tells the person when the bind step refuses', async () => {
		activeModelId.set(null);
		fetchMock.mockResolvedValueOnce(new Response(JSON.stringify({ bound: false, reason: 'limited' }), { status: 200 }));
		await choose();
		await vi.waitFor(() => expect(host.textContent).toContain("Couldn't switch accounts. Try again."));
		expect(reloadModels).not.toHaveBeenCalled();
	});

	it('still pins on a thread that has a model', async () => {
		activeModelId.set('claude-code/opus');
		await choose();
		await vi.waitFor(() => expect(pin).toHaveBeenCalledWith('cond-1', 'acc-2'));
		expect(fetchMock).not.toHaveBeenCalled();
	});

	it('still pins on a thread that is not the CORSIA agent', async () => {
		activeModelId.set(null);
		activeThreadId.set('other');
		await choose();
		await vi.waitFor(() => expect(pin).toHaveBeenCalledWith('other', 'acc-2'));
	});
});
