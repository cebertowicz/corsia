import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

vi.mock('$lib/stores/models', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/models')>()),
	loadModels: vi.fn(),
}));

import OverageSwitch from './OverageSwitch.svelte';
import { loadModels } from '$lib/stores/models';
import { vendorAccounts, type VendorAccount } from '$lib/stores/vendorAccounts';

function account(id: string, allow_overage: boolean | null, provider = 'codex'): VendorAccount {
	return { id, provider, name: id, is_default: false, state: 'signed_in', revision: 1, identity: {}, label: { name: id, email: null, text: id }, allow_overage };
}

async function settle() {
	for (let i = 0; i < 5; i++) {
		await Promise.resolve();
		await tick();
	}
	flushSync();
}

describe('OverageSwitch', () => {
	let components: ReturnType<typeof mount>[] = [];

	afterEach(() => {
		components.forEach((component) => unmount(component));
		components = [];
		document.body.innerHTML = '';
		vendorAccounts.set([]);
		vi.unstubAllGlobals();
		vi.clearAllMocks();
	});

	function render(row: VendorAccount, inline = false): HTMLElement {
		const target = document.createElement('div');
		document.body.appendChild(target);
		components.push(mount(OverageSwitch, { target, props: { account: row, inline } }));
		flushSync();
		return target;
	}

	it('renders the same switch for every account, each reading its own setting', () => {
		const off = render(account('claude-main', false, 'claude-code'));
		const on = render(account('codex-work', true));

		expect(off.textContent).toBe(on.textContent);
		expect(off.textContent).toContain('Spend past usage limit');
		expect(off.querySelector('button')?.getAttribute('aria-pressed')).toBe('false');
		expect(on.querySelector('button')?.getAttribute('aria-pressed')).toBe('true');
	});

	it('names its account when it sits inside an account row', () => {
		expect(render(account('Work', false), true).querySelector('button')?.getAttribute('aria-label'))
			.toBe('Spend past usage limit for Work');
	});

	it('PUTs the flipped value for its own account, stores the returned row, then reloads the catalog', async () => {
		const row = account('acct-2', false);
		vendorAccounts.set([account('acct-1', false), row]);
		const fetchMock = vi.fn().mockResolvedValue(new Response(JSON.stringify({ ...row, allow_overage: true }), { status: 200 }));
		vi.stubGlobal('fetch', fetchMock);
		render(row).querySelector('button')?.click();
		await settle();

		expect(String(fetchMock.mock.calls[0][0])).toMatch(/^\/api\/vendor-accounts\/acct-2\/usage\?/);
		expect(fetchMock.mock.calls[0][1].method).toBe('PUT');
		expect(JSON.parse(fetchMock.mock.calls[0][1].body)).toEqual({ allow_overage: true });
		let stored: VendorAccount[] = [];
		vendorAccounts.subscribe((rows) => { stored = rows; })();
		expect(stored.map((entry) => entry.allow_overage)).toEqual([false, true]);
		expect(loadModels).toHaveBeenCalledTimes(1);
	});

	it('names the switch and direction when the save fails', async () => {
		vi.stubGlobal('fetch', vi.fn().mockRejectedValue(new TypeError('Failed to fetch')));
		const card = render(account('acct-1', true, 'claude-code'));
		card.querySelector('button')?.click();
		await settle();

		expect(card.textContent).toContain('Could not turn Spend past usage limit off: Couldn’t reach the server.');
	});

	it('stays disabled while the account has no plan-limit setting', () => {
		expect(render(account('acct-1', null)).querySelector('button')?.disabled).toBe(true);
	});
});
