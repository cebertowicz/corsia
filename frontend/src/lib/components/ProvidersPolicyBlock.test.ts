import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ProvidersPolicyBlock from './ProvidersPolicyBlock.svelte';
import { token } from '$lib/stores/auth';
import { vendorAccounts } from '$lib/stores/vendorAccounts';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function stubPolicy(policy: object, onSave?: (body: { disabled: string[] }) => object, accounts: object[] | null = []) {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const path = input.toString();
		if (path.startsWith('/api/vendor-accounts')) return accounts
			? { ok: true, json: async () => ({ accounts }) }
			: { ok: false, status: 500, json: async () => ({ detail: 'Accounts unavailable' }) };
		if (init?.method === 'PUT' && onSave) return { ok: true, json: async () => onSave(JSON.parse(init.body as string)) };
		return { ok: true, json: async () => policy };
	}));
}

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('test-token');
	vendorAccounts.set([]);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	token.set(null);
	vi.restoreAllMocks();
});

describe('ProvidersPolicyBlock', () => {
	it('shows loading until a valid empty policy arrives', async () => {
		let resolve!: (value: unknown) => void;
		vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
			if (input.toString().startsWith('/api/vendor-accounts')) return Promise.resolve({ ok: true, json: async () => ({ accounts: [] }) });
			return new Promise((done) => { resolve = done; });
		}));
		component = mount(ProvidersPolicyBlock, { target: host });
		flushSync();
		expect(host.textContent).toContain('Loading providers…');

		resolve({ ok: true, json: async () => ({ scope: 'workspace', providers: [] }) });
		await vi.waitFor(() => expect(host.textContent).not.toContain('Loading providers…'));
		expect(host.textContent).toContain('Providers');
		expect(host.querySelector('[role="alert"]')).toBeNull();
	});

	it('renders the approved project policy states and sign-in affordance', async () => {
		stubPolicy({
			scope: 'project', use_workspace: false, override_revision: 1,
			providers: [
				{ provider_key: 'claude-code', capabilities: { units: 'accounts', rollover: true }, rollover: false, order: null, disabled: [], revision: 1, units: [] },
				{ provider_key: 'codex', capabilities: { units: 'accounts', rollover: true }, rollover: true, order: ['pro', 'lite'], disabled: ['pro'], revision: 1,
					units: [{ id: 'pro', label: 'Pro', enabled: false }, { id: 'lite', label: 'Lite', enabled: true }] },
			],
		});
		const openProviders = vi.fn();
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project', onSignIn: openProviders } });
		flushSync();

		await vi.waitFor(() => expect(host.textContent).toContain("Turning this on replaces this project's settings with the workspace's. Turning it off again starts from the workspace values."));
		expect(host.textContent).toContain('No Claude Code account signed in');
		const signIn = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Sign in') as HTMLButtonElement;
		signIn.click();
		expect(openProviders).toHaveBeenCalledWith('claude-code');
		expect(host.textContent).toContain('Pro');
		expect(host.querySelector('[aria-label="Pro"]')).not.toBeNull();
	});

	it('lets a one-account Claude provider be disabled without rollover or order controls', async () => {
		const provider = { provider_key: 'claude-code', capabilities: { units: 'accounts', rollover: true }, rollover: true, order: ['personal'], disabled: [], revision: 1,
			units: [{ id: 'personal', label: 'Personal Max', enabled: true }] };
		stubPolicy({ scope: 'project', use_workspace: false, override_revision: 1, providers: [provider] }, (body) => ({ ...provider, disabled: body.disabled, units: [{ id: 'personal', label: 'Personal Max', enabled: !body.disabled.includes('personal') }] }));
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.querySelector<HTMLButtonElement>('[aria-label="Personal Max"]')).not.toBeNull());
		const toggle = host.querySelector<HTMLButtonElement>('[aria-label="Personal Max"]')!;
		expect(toggle.getAttribute('aria-pressed')).toBe('true');
		toggle.click();
		await vi.waitFor(() => {
			const save = vi.mocked(fetch).mock.calls.find(([, init]) => init?.method === 'PUT');
			expect(JSON.parse(save?.[1]?.body as string).disabled).toEqual(['personal']);
		});
		expect(host.querySelector('[aria-label="Roll over when an account hits its limit"]')).toBeNull();
		expect(host.querySelector('[aria-label*="Move "]')).toBeNull();
	});

	it('shows a visible error for an unsuccessful policy response', async () => {
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => input.toString().startsWith('/api/vendor-accounts')
			? ({ ok: true, json: async () => ({ accounts: [] }) })
			: ({ ok: false, status: 500, json: async () => ({ detail: 'Policy unavailable' }) })));
		component = mount(ProvidersPolicyBlock, { target: host });
		flushSync();

		await vi.waitFor(() => expect(host.textContent).toContain('Policy unavailable'));
		expect(host.textContent).toContain('Reload providers');
	});

	it('shows a visible error rather than throwing for a malformed successful response', async () => {
		stubPolicy({ scope: 'workspace' });
		component = mount(ProvidersPolicyBlock, { target: host });
		flushSync();

		await vi.waitFor(() => expect(host.textContent).toContain('Could not load providers.'));
		expect(host.textContent).toContain('Reload providers');
	});

	function account(id: string, windows: object[] = []) {
		return { id, provider: 'codex', name: id, is_default: false, state: 'signed_in', revision: 1, identity: {},
			label: { name: id, email: null, text: id, custom: null },
			usage: { state: 'allowed', usable: true, windows, checking: false, check: null } };
	}
	const rollover = (units: object[], order: string[] | null = null, disabled: string[] = []) => ({ scope: 'workspace', overriding_projects: [], providers: [
		{ provider_key: 'codex', capabilities: { units: 'accounts', rollover: true }, rollover: true, order, disabled, revision: 1, units },
	] });
	const rowNames = () => Array.from(host.querySelectorAll('.account-usage-row strong')).map((row) => row.textContent);

	it('lists accounts in the order the server sends, not a reordering of its own', async () => {
		stubPolicy(rollover([{ id: 'b', label: 'b', enabled: true }, { id: 'a', label: 'a', enabled: true }], ['a', 'b']), undefined, [account('a'), account('b')]);
		component = mount(ProvidersPolicyBlock, { target: host });
		flushSync();

		await vi.waitFor(() => expect(rowNames()).toEqual(['b', 'a']));
	});

	it('keeps a switched-off account at its saved place in the order, as the approved design shows', async () => {
		stubPolicy(rollover([{ id: 'a', label: 'a', enabled: true }, { id: 'b', label: 'b', enabled: false }, { id: 'c', label: 'c', enabled: true }], null, ['b']),
			undefined, [account('a'), account('b'), account('c')]);
		component = mount(ProvidersPolicyBlock, { target: host });
		flushSync();

		await vi.waitFor(() => expect(host.querySelectorAll('.account-usage-row')).toHaveLength(3));
		const numbers = Array.from(host.querySelectorAll('.account-usage-row')).map((row) => row.querySelector('.order')?.getAttribute('aria-label') ?? null);
		expect(numbers).toEqual(['Rollover order 1', 'Rollover order 2', 'Rollover order 3']);
	});

	it('reads account usage the way the model picker does', async () => {
		stubPolicy(rollover([{ id: 'a', label: 'a', enabled: true }]), undefined, [account('a', [
			{ kind: 'five_hour', state: 'unknown', used_percent: 40, window_minutes: 300 },
			{ kind: 'seven_day', state: 'reported', used_percent: 99.5, window_minutes: 10080 },
		])]);
		component = mount(ProvidersPolicyBlock, { target: host });
		flushSync();

		await vi.waitFor(() => expect(host.querySelector('.account-usage-row .usage')).not.toBeNull());
		const usage = host.querySelector('.account-usage-row .usage')!;
		expect(usage.textContent?.replace(/\s+/g, ' ').trim()).toBe('5h usage unknown · wk <1% left');
		expect(usage.getAttribute('aria-label')).toBe('Account usage: 5-hour: usage unknown, weekly: less than 1% left');
		expect(usage.querySelector('.red')?.textContent).toBe('wk <1% left');
	});

	it('says so when account details fail to load instead of showing bare rows', async () => {
		stubPolicy(rollover([{ id: 'a', label: 'Work', enabled: true }]), undefined, null);
		component = mount(ProvidersPolicyBlock, { target: host });
		flushSync();

		await vi.waitFor(() => expect(host.querySelector('[role="alert"]')?.textContent).toBe('Accounts unavailable'));
		expect(host.textContent).toContain('Reload providers');
		expect(host.textContent).toContain('Work');
	});
});
