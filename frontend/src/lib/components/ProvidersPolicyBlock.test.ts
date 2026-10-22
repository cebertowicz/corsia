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
			if (input.toString().startsWith('/api/projects/project/cloud-models-off')) return Promise.resolve({ ok: true, json: async () => ({ cloud_models_off: false }) });
			return new Promise((done) => { resolve = done; });
		}));
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();
		expect(host.textContent).toContain('Loading providers…');

		resolve({ ok: true, json: async () => ({ scope: 'workspace', providers: [] }) });
		await vi.waitFor(() => expect(host.textContent).not.toContain('Loading providers…'));
		expect(host.textContent).toContain('Accounts and rollover');
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

		await vi.waitFor(() => expect(host.textContent).toContain("Turning this on replaces this project's settings with the workspace's."));
		expect(host.textContent).toContain('No Claude Code account signed in');
		const signIn = Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === 'Sign in') as HTMLButtonElement;
		signIn.click();
		expect(openProviders).toHaveBeenCalledWith('claude-code');
		expect(host.textContent).toContain('Pro');
		expect(host.querySelector('[aria-label="Use Pro"]')).not.toBeNull();
	});

	it('uses the same workspace-setting description while inheriting', async () => {
		stubPolicy({ scope: 'project', use_workspace: true, override_revision: 1, providers: [] });
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.textContent).toContain("Use the workspace's account order and rollover."));
		expect(host.textContent).not.toContain('Turning this on replaces');
	});

	it('explains what turning workspace settings back on replaces, only while the project has its own', async () => {
		stubPolicy({ scope: 'project', use_workspace: false, override_revision: 1, providers: [] });
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.textContent).toContain("Turning this on replaces this project's settings with the workspace's. Turning it off again starts from the workspace values."));
		expect(host.textContent).not.toContain("Use the workspace's account order and rollover.");
	});

	it('tells the person to check the connection when an account switch cannot reach the server', async () => {
		const provider = { provider_key: 'claude-code', capabilities: { units: 'accounts' as const, rollover: true }, rollover: true, order: null, disabled: [], revision: 1,
			units: [{ id: 'account-a', label: 'Account A', enabled: true }, { id: 'account-b', label: 'Account B', enabled: true }] };
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const path = input.toString();
			if (init?.method === 'PUT') throw new TypeError('Failed to fetch');
			if (path.startsWith('/api/vendor-accounts')) return { ok: true, json: async () => ({ accounts: [] }) };
			if (path.startsWith('/api/projects/project/cloud-models-off')) return { ok: true, json: async () => ({ cloud_models_off: false }) };
			return { ok: true, json: async () => ({ scope: 'project', use_workspace: false, override_revision: 1, providers: [provider] }) };
		}));
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.querySelector<HTMLButtonElement>('[aria-label="Use Account A"]')).not.toBeNull());
		host.querySelector<HTMLButtonElement>('[aria-label="Use Account A"]')!.click();
		await vi.waitFor(() => expect(host.querySelector('.policy-status.error')?.textContent).toBe('Couldn’t save this change. Check your connection and try again.'));
	});

	it('shows both load errors above a single Reload providers button', async () => {
		const provider = { provider_key: 'claude-code', capabilities: { units: 'accounts' as const, rollover: true }, rollover: true, order: null, disabled: [], revision: 1,
			units: [{ id: 'account-a', label: 'Account A', enabled: true }, { id: 'account-b', label: 'Account B', enabled: true }] };
		let policyReads = 0;
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const path = input.toString();
			if (init?.method === 'PUT') return { ok: false, status: 409, json: async () => ({ detail: 'changed', code: 'policy_changed' }) };
			if (path.startsWith('/api/vendor-accounts')) return { ok: false, status: 500, json: async () => ({ detail: 'Accounts unavailable' }) };
			if (path.startsWith('/api/projects/project/cloud-models-off')) return { ok: true, json: async () => ({ cloud_models_off: false }) };
			if (policyReads++ > 0) throw new TypeError('Failed to fetch');
			return { ok: true, json: async () => ({ scope: 'project', use_workspace: false, override_revision: 1, providers: [provider] }) };
		}));
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.querySelector<HTMLButtonElement>('[aria-label="Use Account A"]')).not.toBeNull());
		host.querySelector<HTMLButtonElement>('[aria-label="Use Account A"]')!.click();
		await vi.waitFor(() => expect(host.querySelectorAll('p.error[role="alert"]')).toHaveLength(2));
		expect([...host.querySelectorAll('button')].filter((button) => button.textContent?.trim() === 'Reload providers')).toHaveLength(1);
	});

	it('keeps the error and a Reload providers button when the policy cannot be re-read after a failed save', async () => {
		const provider = { provider_key: 'claude-code', capabilities: { units: 'accounts' as const, rollover: true }, rollover: true, order: null, disabled: [], revision: 1,
			units: [{ id: 'account-a', label: 'Account A', enabled: true }, { id: 'account-b', label: 'Account B', enabled: true }] };
		let policyReads = 0;
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const path = input.toString();
			if (init?.method === 'PUT') return { ok: false, status: 409, json: async () => ({ detail: 'provider policy revision changed; refresh before acting', code: 'policy_changed' }) };
			if (path.startsWith('/api/vendor-accounts')) return { ok: true, json: async () => ({ accounts: [] }) };
			if (path.startsWith('/api/projects/project/cloud-models-off')) return { ok: true, json: async () => ({ cloud_models_off: false }) };
			if (policyReads++ > 0 && policyReads < 3) throw new TypeError('Failed to fetch');
			return { ok: true, json: async () => ({ scope: 'project', use_workspace: false, override_revision: 1, providers: [provider] }) };
		}));
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.querySelector<HTMLButtonElement>('[aria-label="Use Account A"]')).not.toBeNull());
		host.querySelector<HTMLButtonElement>('[aria-label="Use Account A"]')!.click();
		await vi.waitFor(() => expect(host.querySelector('.policy-status.error')?.textContent).toBe('Couldn’t save this change. Check your connection and try again.'));
		const reload = [...host.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Reload providers');
		expect(reload).toBeDefined();
		expect(host.querySelector('p.error[role="alert"]')).not.toBeNull();
		reload!.click();
		await vi.waitFor(() => expect(host.querySelector('p.error[role="alert"]')).toBeNull());
		expect(host.querySelector('[aria-label="Use Account A"]')).not.toBeNull();
	});

	it('lets a one-account Claude provider be disabled without rollover or order controls', async () => {
		const provider = { provider_key: 'claude-code', capabilities: { units: 'accounts', rollover: true }, rollover: true, order: ['personal'], disabled: [], revision: 1,
			units: [{ id: 'personal', label: 'Personal Max', enabled: true }] };
		stubPolicy({ scope: 'project', use_workspace: false, override_revision: 1, providers: [provider] }, (body) => ({ ...provider, disabled: body.disabled, units: [{ id: 'personal', label: 'Personal Max', enabled: !body.disabled.includes('personal') }] }));
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.querySelector<HTMLButtonElement>('[aria-label="Use Personal Max"]')).not.toBeNull());
		const toggle = host.querySelector<HTMLButtonElement>('[aria-label="Use Personal Max"]')!;
		expect(toggle.getAttribute('aria-pressed')).toBe('true');
		toggle.click();
		await vi.waitFor(() => {
			const save = vi.mocked(fetch).mock.calls.find(([, init]) => init?.method === 'PUT');
			expect(JSON.parse(save?.[1]?.body as string).disabled).toEqual(['personal']);
		});
		expect(host.querySelector('[aria-label="Roll over when an account hits its limit"]')).toBeNull();
		expect(host.querySelector('[aria-label*="Move "]')).toBeNull();
	});

	it('keeps both account toggles usable after saving either account', async () => {
		const provider = { provider_key: 'codex', capabilities: { units: 'accounts' as const, rollover: true }, rollover: true, order: null, disabled: [], revision: 1,
			units: [{ id: 'account-a', label: 'Account A', enabled: true }, { id: 'account-b', label: 'Account B', enabled: true }] };
		stubPolicy({ scope: 'workspace', overriding_projects: [], providers: [provider] }, (body) => ({
			...provider,
			disabled: body.disabled,
			revision: provider.revision + 1,
			units: provider.units.map((unit) => ({ ...unit, enabled: !body.disabled.includes(unit.id) })),
		}));
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.querySelector<HTMLButtonElement>('[aria-label="Use Account A"]')).not.toBeNull());
		const accountA = host.querySelector<HTMLButtonElement>('[aria-label="Use Account A"]')!;
		accountA.click();
		await vi.waitFor(() => expect(vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === 'PUT')).toHaveLength(1));
		const accountB = host.querySelector<HTMLButtonElement>('[aria-label="Use Account B"]')!;
		await vi.waitFor(() => expect(accountB.disabled).toBe(false));
		accountB.click();
		await vi.waitFor(() => expect(vi.mocked(fetch).mock.calls.filter(([, init]) => init?.method === 'PUT')).toHaveLength(2));
		await vi.waitFor(() => expect(accountB.disabled).toBe(false));
	});

	it('shows a visible error for an unsuccessful policy response', async () => {
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => input.toString().startsWith('/api/vendor-accounts')
			? ({ ok: true, json: async () => ({ accounts: [] }) })
			: ({ ok: false, status: 500, json: async () => ({ detail: 'Policy unavailable' }) })));
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.textContent).toContain('Policy unavailable'));
		expect(host.textContent).toContain('Reload providers');
	});

	it('shows a visible error rather than throwing for a malformed successful response', async () => {
		stubPolicy({ scope: 'workspace' });
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.textContent).toContain('Could not load providers.'));
		expect(host.textContent).toContain('Reload providers');
	});

	const projectPolicy = {
		scope: 'project', use_workspace: false, override_revision: 1, providers: [
			{ provider_key: 'claude-code', capabilities: { units: 'accounts', rollover: true }, rollover: false, order: null, disabled: [], revision: 1, units: [{ id: 'personal', label: 'Personal', enabled: true }] },
			{ provider_key: 'codex', capabilities: { units: 'accounts', rollover: true }, rollover: false, order: null, disabled: [], revision: 1, units: [] },
		],
	};
	function stubProjectCloudOff(initial: boolean, save: 'ok' | 'fail' = 'ok', policy: object = projectPolicy) {
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const path = input.toString();
			if (path.startsWith('/api/vendor-accounts')) return { ok: true, json: async () => ({ accounts: [] }) };
			if (path.startsWith('/api/threads?')) return { ok: true, json: async () => ({ threads: [] }) };
			if (path.startsWith('/api/projects/project/cloud-models-off')) {
				if (init?.method === 'PUT') return save === 'ok'
					? { ok: true, json: async () => ({ cloud_models_off: true }) }
					: { ok: false, status: 500, json: async () => ({ detail: 'Save denied' }) };
				return { ok: true, json: async () => ({ cloud_models_off: initial }) };
			}
			return { ok: true, json: async () => policy };
		}));
	}

	it('names exactly the account providers the policy lists in the Local models only line', async () => {
		stubProjectCloudOff(true, 'ok', { ...projectPolicy, providers: [projectPolicy.providers[1]] });
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.textContent).toContain("Codex isn't used while Local models only is on."));
		expect(host.textContent).not.toContain('Claude Code');
	});

	it('hides project account controls behind one quiet line only when saved Local models only is on', async () => {
		stubProjectCloudOff(true);
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.textContent).toContain("Claude Code and Codex aren't used while Local models only is on."));
		expect(host.textContent).not.toContain('Use workspace settings');
		expect(host.querySelector('[aria-label="Use Personal"]')).toBeNull();
	});

	it('shows project accounts as today when saved Local models only is off', async () => {
		stubProjectCloudOff(false);
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.querySelector('[aria-label="Use Personal"]')).not.toBeNull());
		expect(host.textContent).toContain('Use workspace settings');
	});

	it('swaps account controls only after saving Local models only succeeds', async () => {
		stubProjectCloudOff(false);
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Use Personal"]')).not.toBeNull());

		(host.querySelector('[aria-label="Local models only"]') as HTMLButtonElement).click();
		await vi.waitFor(() => expect(host.textContent).toContain("Claude Code and Codex aren't used while Local models only is on."));
		expect(vi.mocked(fetch).mock.calls.some(([path]) => String(path).startsWith('/api/threads?'))).toBe(true);
	});

	it('keeps account controls visible when saving Local models only fails', async () => {
		stubProjectCloudOff(false, 'fail');
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Use Personal"]')).not.toBeNull());

		(host.querySelector('[aria-label="Local models only"]') as HTMLButtonElement).click();
		await vi.waitFor(() => expect(host.querySelector('[role="alert"]')?.textContent).toContain('Couldn’t save this change.'));
		expect(host.querySelector('[aria-label="Use Personal"]')).not.toBeNull();
	});

	it('keeps account controls visible when loading Local models only fails', async () => {
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const path = input.toString();
			if (path.startsWith('/api/vendor-accounts')) return { ok: true, json: async () => ({ accounts: [] }) };
			if (path.startsWith('/api/projects/project/cloud-models-off')) return { ok: false, status: 500, json: async () => ({ detail: 'Read denied' }) };
			return { ok: true, json: async () => projectPolicy };
		}));
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.querySelector('[aria-label="Use Personal"]')).not.toBeNull());
		// The setting's value is unknown, so its switch is hidden rather than shown as off.
		expect(host.querySelector('[aria-label="Local models only"]')).toBeNull();
		expect(host.querySelector('[role="alert"]')?.textContent).toContain('Couldn’t load this setting. Try again');
	});

	it('keeps the existing loading line until Local models only has loaded', async () => {
		let resolveCloud!: (value: unknown) => void;
		vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
			const path = input.toString();
			if (path.startsWith('/api/vendor-accounts')) return Promise.resolve({ ok: true, json: async () => ({ accounts: [] }) });
			if (path.startsWith('/api/projects/project/cloud-models-off')) return new Promise((resolve) => { resolveCloud = resolve; });
			return Promise.resolve({ ok: true, json: async () => projectPolicy });
		}));
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.textContent).toContain('Loading providers…'));
		expect(host.querySelector('[aria-label="Use Personal"]')).toBeNull();
		resolveCloud({ ok: true, json: async () => ({ cloud_models_off: false }) });
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Use Personal"]')).not.toBeNull());
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
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(rowNames()).toEqual(['b', 'a']));
	});

	it('keeps a switched-off account at its saved place in the order, as the approved design shows', async () => {
		stubPolicy(rollover([{ id: 'a', label: 'a', enabled: true }, { id: 'b', label: 'b', enabled: false }, { id: 'c', label: 'c', enabled: true }], null, ['b']),
			undefined, [account('a'), account('b'), account('c')]);
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
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
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.querySelector('.account-usage-row .usage')).not.toBeNull());
		const usage = host.querySelector('.account-usage-row .usage')!;
		expect(usage.textContent?.replace(/\s+/g, ' ').trim()).toBe('5h usage unknown · wk <1% left');
		expect(usage.getAttribute('aria-label')).toBe('Account usage: 5-hour: usage unknown, weekly: less than 1% left');
		expect(usage.querySelector('.red')?.textContent).toBe('wk <1% left');
	});

	it('says so when account details fail to load instead of showing bare rows', async () => {
		stubPolicy(rollover([{ id: 'a', label: 'Work', enabled: true }]), undefined, null);
		component = mount(ProvidersPolicyBlock, { target: host, props: { projectId: 'project' } });
		flushSync();

		await vi.waitFor(() => expect(host.querySelector('[role="alert"]')?.textContent).toBe('Accounts unavailable'));
		expect(host.textContent).toContain('Reload providers');
		expect(host.textContent).toContain('Work');
	});
});
