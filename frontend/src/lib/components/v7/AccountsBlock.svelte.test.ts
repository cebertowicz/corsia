import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import AccountsBlock from './AccountsBlock.svelte';
import { checkVendorSignin, loadVendorAccounts, removeVendorAccount, renameVendorAccount, signOutVendorAccount, type VendorAccount, vendorAccounts } from '$lib/stores/vendorAccounts';
import { get } from 'svelte/store';
import { token } from '$lib/stores/auth';

describe('rendering and actions', () => {
	const provider = {
		name: 'codex', label: 'Codex', type: 'harness' as const, statusDot: 'online' as const,
		subline: '4 models', raw: { name: 'codex', label: 'Codex', type: 'harness' as const, has_key: false, masked_key: null, api_base: null, docs_url: null, models: [] },
	};

	const defaultAccount = {
		id: 'default', provider: 'codex', name: 'Default', is_default: true, state: 'legacy' as const,
		revision: 1, identity: {}, label: { name: 'Plus account 1', email: null, text: 'Plus account 1', custom: null },
	};
	const workAccount = {
		id: 'work', provider: 'codex', name: 'Work', is_default: false, state: 'signed_out' as const,
		revision: 2, identity: { email: 'work@example.test' }, label: { name: 'Work', email: 'work@example.test', text: 'Work · work@example.test', custom: 'Work' },
	};

	function response(body: unknown, status = 200): Response {
		return new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
	}

	function button(label: string): HTMLButtonElement {
		const found = [...document.querySelectorAll('button')].find((element) => element.textContent?.trim() === label);
		if (!found) throw new Error(`No ${label} button`);
		return found as HTMLButtonElement;
	}

	async function settle(): Promise<void> {
		await new Promise((resolve) => setTimeout(resolve, 0));
		flushSync();
	}

	async function openAccountMenu(label: string): Promise<void> {
		const row = [...document.querySelectorAll<HTMLElement>('[id^="vendor-account-"]')].find((candidate) => candidate.textContent?.includes(label));
		if (!row) throw new Error(`No account row for ${label}`);
		row.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 8, clientY: 8 }));
		await settle();
	}

	afterEach(() => {
		token.set(null);
		vendorAccounts.set([]);
		vi.unstubAllGlobals();
		document.body.innerHTML = '';
	});

	describe('AccountsBlock', () => {
		it('shows the single account in the Accounts block', async () => {
			vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ accounts: [defaultAccount] })));
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle();
			expect(document.body.textContent).not.toContain('Accounts');
			expect(document.body.textContent).toContain('Plus account 1');
			expect(document.body.textContent).not.toContain('Default');
			expect(document.body.textContent).toContain('Add account');
			unmount(component);
		});

		it('keeps the account row a plain menu trigger while exposing its policy toggle by account name', async () => {
			token.set('test-token');
			vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
				const path = typeof input === 'string' ? input : input instanceof URL ? input.pathname : input.url;
				return path.startsWith('/api/provider-policy')
					? Promise.resolve(response({ providers: [{ provider_key: 'codex', capabilities: { units: 'accounts', rollover: false }, rollover: false, order: null, disabled: [], revision: 1, units: [{ id: 'default', label: 'Plus account 1', enabled: true }] }] }))
					: Promise.resolve(response({ accounts: [defaultAccount] }));
			}));
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle();
			const row = document.querySelector<HTMLElement>('#vendor-account-default')!;
			expect(row.getAttribute('role')).toBeNull();
			expect(row.getAttribute('aria-label')).toBeNull();
			await vi.waitFor(() => expect(document.querySelector('[aria-label="Use Plus account 1"]')).not.toBeNull());
			row.focus();
			row.dispatchEvent(new KeyboardEvent('keydown', { key: 'ContextMenu', bubbles: true }));
			await settle();
			expect(document.querySelector('[role="menu"]')?.textContent).toContain('Rename');
			unmount(component);
		});

		it('keeps a failed workspace policy control at its saved value and reports the next action below the account row', async () => {
			token.set('test-token');
			vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
				const path = typeof input === 'string' ? input : input instanceof URL ? input.pathname : input.url;
				if (init?.method === 'PUT') return Promise.resolve(response({ detail: 'Could not save subscriptions.' }, 500));
				return path.startsWith('/api/provider-policy')
					? Promise.resolve(response({ providers: [{ provider_key: 'codex', capabilities: { units: 'accounts', rollover: false }, rollover: false, order: null, disabled: [], revision: 1, units: [{ id: 'default', label: 'Plus account 1', enabled: true }] }] }))
					: Promise.resolve(response({ accounts: [defaultAccount] }));
			}));
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await vi.waitFor(() => expect(document.querySelector<HTMLButtonElement>('[aria-label="Use Plus account 1"]')).not.toBeNull());
			const toggle = document.querySelector<HTMLButtonElement>('[aria-label="Use Plus account 1"]')!;
			toggle.click();
			await vi.waitFor(() => expect(document.body.textContent).toContain("Couldn’t save this change. Try again."));
			expect(toggle.getAttribute('aria-pressed')).toBe('true');
			expect(document.getElementById('account-policy-Plus-account-1')?.closest('.account-usage-row')).not.toBeNull();
			expect(document.querySelector('.account-usage-row .policy-status')?.textContent).toBe("Couldn’t save this change. Try again.");
			expect(document.body.textContent).not.toContain('Could not save subscriptions.');
			unmount(component);
		});

		describe('workspace account policy', () => {
			const signedIn = (id: string, name: string) => ({ ...workAccount, id, name, state: 'signed_in' as const, label: { name, email: null, text: name, custom: name } });
			const entry = (ids: string[], order: string[] | null = null) => ({
				provider_key: 'codex', capabilities: { units: 'accounts', rollover: true }, rollover: true, order, disabled: [], revision: 1,
				units: ids.map((id) => ({ id, label: id, enabled: true })),
			});
			const rowIds = () => [...document.querySelectorAll('[id^="vendor-account-"]')].map((row) => row.id.replace('vendor-account-', ''));
			const numbers = () => [...document.querySelectorAll('.account-usage-row .order')].map((node) => node.textContent);
			const moveButton = (name: string, direction: 'up' | 'down') => document.querySelector<HTMLButtonElement>(`[aria-label="Move ${name} ${direction}"]`)!;

			it('shows the saved rollover order, and a move visibly swaps the rows it acts on', async () => {
				token.set('test-token');
				// The accounts list order differs from the saved order: c, a, b.
				let units = ['c', 'a', 'b'];
				let puts: unknown[] = [];
				vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL, init?: RequestInit) => {
					const path = typeof input === 'string' ? input : input instanceof URL ? input.pathname : input.url;
					if (init?.method === 'PUT') {
						const body = JSON.parse(String(init.body));
						puts.push(body);
						units = body.order;
						return Promise.resolve(response(entry(units, units)));
					}
					return path.startsWith('/api/provider-policy')
						? Promise.resolve(response({ providers: [entry(units, units)] }))
						: Promise.resolve(response({ accounts: [signedIn('a', 'Alpha'), signedIn('b', 'Bravo'), signedIn('c', 'Charlie')] }));
				}));
				const component = mount(AccountsBlock, { target: document.body, props: { provider } });
				await vi.waitFor(() => expect(rowIds()).toEqual(['c', 'a', 'b']));
				await vi.waitFor(() => expect(numbers()).toEqual(['1', '2', '3']));
				expect(moveButton('Charlie', 'up').disabled).toBe(true);
				expect(moveButton('Charlie', 'down').disabled).toBe(false);
				expect(moveButton('Bravo', 'up').disabled).toBe(false);
				expect(moveButton('Bravo', 'down').disabled).toBe(true);

				moveButton('Charlie', 'down').click();
				await vi.waitFor(() => expect(rowIds()).toEqual(['a', 'c', 'b']));
				expect(puts).toEqual([expect.objectContaining({ order: ['a', 'c', 'b'] })]);
				expect(moveButton('Alpha', 'up').disabled).toBe(true);
				expect(moveButton('Charlie', 'up').disabled).toBe(false);
				unmount(component);
			});

			it('numbers only the units and puts an account with no unit after them', async () => {
				token.set('test-token');
				const removed = { ...signedIn('gone', 'Removed one'), state: 'removed' as const };
				vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
					const path = typeof input === 'string' ? input : input instanceof URL ? input.pathname : input.url;
					if (path.startsWith('/api/provider-policy')) return Promise.resolve(response({ providers: [entry(['b', 'a'], ['b', 'a'])] }));
					return Promise.resolve(response({ accounts: [removed, signedIn('a', 'Alpha'), signedIn('b', 'Bravo')] }));
				}));
				const component = mount(AccountsBlock, { target: document.body, props: { provider } });
				await vi.waitFor(() => expect(numbers()).toEqual(['1', '2']));
				expect(rowIds()).toEqual(['b', 'a']);
				unmount(component);
			});

			it('reloads the policy when the vendor accounts change, so a new account gets its switch and rollover appears', async () => {
				token.set('test-token');
				let accounts = [signedIn('a', 'Alpha')];
				let units = ['a'];
				vi.stubGlobal('fetch', vi.fn((input: RequestInfo | URL) => {
					const path = typeof input === 'string' ? input : input instanceof URL ? input.pathname : input.url;
					return path.startsWith('/api/provider-policy')
						? Promise.resolve(response({ providers: [entry(units)] }))
						: Promise.resolve(response({ accounts }));
				}));
				const component = mount(AccountsBlock, { target: document.body, props: { provider } });
				await vi.waitFor(() => expect(document.querySelector('[aria-label="Use Alpha"]')).not.toBeNull());
				expect(document.body.textContent).not.toContain('Roll over when an account hits its limit');

				accounts = [signedIn('a', 'Alpha'), signedIn('b', 'Bravo')];
				units = ['a', 'b'];
				await loadVendorAccounts();
				await vi.waitFor(() => expect(document.querySelector('[aria-label="Use Bravo"]')).not.toBeNull());
				expect(document.body.textContent).toContain('Roll over when an account hits its limit');
				expect(numbers()).toEqual(['1', '2']);
				unmount(component);
			});
		});

		it('renders limit, checking, and verification messages independently for every usage state', async () => {
			const cases = [
				[{ state: 'unknown', usable: false, resets_at: null, windows: [], checking: true, check: null }, ['Checking account…']],
				[{ state: 'limited', usable: false, reason: 'five_hour_limit', resets_at: null, windows: [], checking: false, check: { error: 'offline' } }, ['Usage limit reached · reset time unknown', "Couldn't verify account"]],
				[{ state: 'allowed', usable: true, resets_at: null, windows: [], checking: true, check: null }, ['Checking account…']],
			] as const;
			for (const [usage, expected] of cases) {
				vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ accounts: [{ ...defaultAccount, usage }] })));
				const component = mount(AccountsBlock, { target: document.body, props: { provider } });
				await settle();
				for (const text of expected) expect(document.body.textContent).toContain(text);
				unmount(component);
				document.body.innerHTML = '';
			}
		});

		it('prefills rename with a named account custom value only', async () => {
			vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ accounts: [defaultAccount, workAccount] })));
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle();
			await openAccountMenu('Work · work@example.test');
			button('Rename').click();
			await settle();
			const input = document.querySelector('[aria-label="Rename Work · work@example.test"]') as HTMLInputElement;
			expect(input.value).toBe('Work');
			expect(input.placeholder).toBe('Work · work@example.test');
			unmount(component);
		});

		it('prefills rename empty for an unnamed account while retaining its label placeholder', async () => {
			vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ accounts: [defaultAccount] })));
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle();
			await openAccountMenu('Plus account 1');
			button('Rename').click();
			await settle();
			const input = document.querySelector('[aria-label="Rename Plus account 1"]') as HTMLInputElement;
			expect(input.value).toBe('');
			expect(input.placeholder).toBe('Plus account 1');
			unmount(component);
		});

		it('keeps each account overage switch in its row', async () => {
			vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ accounts: [{ ...defaultAccount, allow_overage: true }] })));
			const single = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle();
			const only = [...document.querySelectorAll('button[aria-label^="Spend past plan limit"]')];
			expect(only.map((element) => [element.getAttribute('aria-label'), element.getAttribute('aria-pressed')]))
				.toEqual([['Spend past plan limit for Plus account 1', 'true']]);
			unmount(single);

			vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ accounts: [
				{ ...defaultAccount, allow_overage: false }, { ...workAccount, state: 'signed_in', allow_overage: true },
			] })));
			const listed = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle();
			const switches = [...document.querySelectorAll('button[aria-label^="Spend past plan limit"]')];
			expect(switches.map((element) => [element.getAttribute('aria-label'), element.getAttribute('aria-pressed')]))
				.toEqual([['Spend past plan limit for Plus account 1', 'false'], ['Spend past plan limit for Work · work@example.test', 'true']]);
			unmount(listed);
		});

		it('uses the backend label when email is absent', async () => {
			vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ accounts: [
				{ ...defaultAccount, label: { name: 'Personal', email: 'person@example.test', text: 'Personal · person@example.test' } },
				{ ...workAccount, state: 'signed_in', label: { name: 'Plus account 2', email: null, text: 'Plus account 2' } },
			] })));
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle();
			expect(document.querySelector('.account-usage-row .name')?.textContent).toBe('Personal');
			expect(document.querySelector('.account-usage-row .email')?.textContent).toBe('person@example.test');
			expect(document.body.textContent).toContain('Plus account 2');
			unmount(component);
		});

		it('does not repeat an email when it is already the account label', async () => {
			vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ accounts: [
				{ ...defaultAccount, label: { name: 'me@example.test', email: 'me@example.test', text: 'me@example.test', custom: null } },
			] })));
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle();
			expect(document.querySelectorAll('.account-usage-row .email')).toHaveLength(0);
			expect(document.body.textContent?.match(/me@example\.test/g)).toHaveLength(1);
			unmount(component);
		});

		it('shows a waiting sign-in only after the explicit action, with Check and Cancel', async () => {
			const fetch = vi.fn()
				.mockResolvedValueOnce(response({ accounts: [defaultAccount, workAccount] }))
				.mockResolvedValueOnce(response({ detail: 'no sign-in is in progress for this account' }, 409))
				.mockResolvedValueOnce(response({ account_id: 'work', route: 'browser', status: 'waiting', cause: null }));
			vi.stubGlobal('fetch', fetch);
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle();
			button('Sign in').click();
			await settle();
			expect(document.body.textContent).toContain('Waiting for sign-in in your browser');
			expect(document.body.textContent).toContain('Check');
			expect(document.body.textContent).toContain('Cancel');
			expect(fetch).toHaveBeenCalledTimes(3);
			unmount(component);
		});

		it('names and focuses the account holding a refused sign-in', async () => {
			const fetch = vi.fn()
				.mockResolvedValueOnce(response({ accounts: [defaultAccount, workAccount] }))
				.mockResolvedValueOnce(response({ detail: 'no sign-in is in progress for this account' }, 409))
				.mockResolvedValueOnce(response({ account_id: 'work', route: 'browser', status: 'refused', cause: 'codex_signin_in_progress', held_by: 'default' }));
			vi.stubGlobal('fetch', fetch);
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle();
			button('Sign in').click();
			await settle();
			expect(document.body.textContent).toContain('Plus account 1 is already signing in');
			expect(document.activeElement?.id).toBe('vendor-account-default');
			unmount(component);
		});

		it('names the existing account when a sign-in reaches the same account', async () => {
			const fetch = vi.fn()
				.mockResolvedValueOnce(response({ accounts: [defaultAccount, workAccount] }))
				.mockResolvedValueOnce(response({ detail: 'no sign-in is in progress for this account' }, 409))
				.mockResolvedValueOnce(response({ account_id: 'work', route: 'browser', status: 'failed', cause: 'same_account', duplicate_of: 'default' }));
			vi.stubGlobal('fetch', fetch);
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle();
			button('Sign in').click();
			await settle();
			expect(document.body.textContent).toContain('Already signed in as Plus account 1');
			unmount(component);
		});

		it('keeps a sole signed-out account visible and checks any reopened sign-in once', async () => {
			const fetch = vi.fn()
				.mockResolvedValueOnce(response({ accounts: [workAccount] }))
				.mockResolvedValueOnce(response({ detail: 'no sign-in is in progress for this account' }, 409));
			vi.stubGlobal('fetch', fetch);
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle();
			expect(document.body.textContent).toContain('Work');
			expect(document.body.textContent).toContain('Signed out');
			expect(document.body.textContent).toContain('Sign in');
			expect(fetch).toHaveBeenCalledTimes(2);
			unmount(component);
		});

		it('does not expose custom sign-out or removal for a provider-managed account', async () => {
			const fetch = vi.fn()
				.mockResolvedValueOnce(response({ accounts: [defaultAccount, workAccount] }))
				.mockResolvedValueOnce(response({ detail: 'no sign-in is in progress for this account' }, 409));
			vi.stubGlobal('fetch', fetch);
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle();
			await openAccountMenu('Plus account 1');
			const menu = document.querySelector('[role="menu"]')?.textContent;
			expect(menu).toContain('Rename');
			expect(menu).not.toContain('Sign out');
			expect(menu).not.toContain('Remove');
			unmount(component);
		});

		it('keeps the sign-out result direction when its reload fails', async () => {
			const signedIn = { ...workAccount, state: 'signed_in' as const, revision: 3 };
			const fetch = vi.fn()
				.mockResolvedValueOnce(response({ accounts: [defaultAccount, signedIn] }))
				.mockResolvedValueOnce(response({ ...signedIn, state: 'signed_out', revision: 4 }))
				.mockResolvedValueOnce(response({ detail: 'backend offline' }, 503));
			vi.stubGlobal('fetch', fetch);
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle();
			await openAccountMenu('Work · work@example.test');
			button('Sign out').click();
			await settle();
			expect(document.body.textContent).toContain('The account was signed out. The account list could not be reloaded.');
			expect(fetch.mock.calls[1][1].method).toBe('POST');
			unmount(component);
		});

		it('refreshes a completed sign-in before a following rename', async () => {
			const signedIn = { ...workAccount, state: 'signed_in' as const, revision: 3 };
			const renamed = { ...signedIn, name: 'Renamed', revision: 4, label: { name: 'Renamed', email: 'work@example.test', text: 'Renamed · work@example.test' } };
			const fetch = vi.fn()
				.mockResolvedValueOnce(response({ accounts: [defaultAccount, workAccount] }))
				.mockResolvedValueOnce(response({ detail: 'no sign-in is in progress for this account' }, 409))
				.mockResolvedValueOnce(response({ account_id: 'work', route: 'browser', status: 'waiting', cause: null }))
				.mockResolvedValueOnce(response({ account_id: 'work', route: 'browser', status: 'completed', cause: null }))
				.mockResolvedValueOnce(response({ accounts: [defaultAccount, signedIn] }))
				.mockResolvedValueOnce(response(renamed));
			vi.stubGlobal('fetch', fetch);
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle(); button('Sign in').click(); await settle(); button('Check').click(); await settle();
			await openAccountMenu('Work · work@example.test');
			button('Rename').click(); await settle();
			const input = document.querySelector('[aria-label="Rename Work · work@example.test"]') as HTMLInputElement;
			input.value = 'Renamed'; input.dispatchEvent(new Event('input', { bubbles: true })); await settle();
			button('Save').click(); await settle();
			expect(fetch.mock.calls[5][1].body).toContain('"expected_revision":3');
			expect(fetch.mock.calls[5][1].body).toContain('"name":"Renamed"');
			expect(document.querySelector('#vendor-account-work')?.textContent).toContain('Renamed');
			unmount(component);
		});

		it.each(['failed', 'cancelled'] as const)('offers one recovery action after %s sign-in', async (status) => {
			const fetch = vi.fn()
				.mockResolvedValueOnce(response({ accounts: [defaultAccount, workAccount] }))
				.mockResolvedValueOnce(response({ detail: 'no sign-in is in progress for this account' }, 409))
				.mockResolvedValueOnce(response({ account_id: 'work', route: 'browser', status, cause: 'vendor_login_failed' }));
			vi.stubGlobal('fetch', fetch);
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle(); button('Sign in').click(); await settle();
			expect([...document.querySelectorAll('button')].filter((item) => item.textContent?.trim().startsWith('Sign in')).map((item) => item.textContent?.trim())).toEqual(['Sign in again']);
			unmount(component);
		});

		it('keeps provider-managed defaults visible without mutable default controls', async () => {
			const providerDefault = { ...workAccount, state: 'signed_in' as const, is_default: true, revision: 3 };
			vi.stubGlobal('fetch', vi.fn().mockResolvedValue(response({ accounts: [defaultAccount, providerDefault] })));
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle();
			expect(document.body.textContent).toContain('Plus account 1');
			expect(document.body.textContent).not.toContain('Default');
			expect(document.body.textContent).not.toContain('Set default');
			expect(document.body.textContent).not.toContain('Clear default');
			unmount(component);
		});
	});
});

describe('lifecycle', () => {
	const provider = { name: 'codex', label: 'Codex', type: 'harness' as const, statusDot: 'online' as const, subline: '2 models', raw: { name: 'codex', label: 'Codex', type: 'harness' as const, has_key: false, masked_key: null, api_base: null, docs_url: null, models: [] } };
	const ambient = { id: 'default', provider: 'codex', name: 'Default', state: 'legacy', is_default: true, revision: 0, identity: {}, label: { name: 'Plus account 1', email: null, text: 'Plus account 1' } };
	const out = { id: 'work', provider: 'codex', name: 'Work', state: 'signed_out', is_default: false, revision: 0, identity: {}, label: { name: 'Work', email: null, text: 'Work' } };
	const inside = { ...out, state: 'signed_in', revision: 1 };
	const waiting = { account_id: 'work', route: 'browser', status: 'waiting', cause: null };
	const completed = { ...waiting, status: 'completed' };
	const missing = { detail: 'no sign-in is in progress for this account' };
	function res(body: unknown, status = 200) { return new Response(JSON.stringify(body), { status }); }
	function list(account = out) { return res({ accounts: [ambient, account] }); }
	function button(name: string) { const el = [...document.querySelectorAll<HTMLButtonElement>('button')].find((b) => b.textContent?.trim() === name); if (!el) throw new Error(`Missing ${name}`); return el; }
	async function settle() { await new Promise((r) => setTimeout(r, 0)); flushSync(); }
	async function more() { const row = document.querySelector<HTMLElement>('#vendor-account-work')!; row.dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 8, clientY: 8 })); await settle(); }
	afterEach(() => { vendorAccounts.set([]); vi.unstubAllGlobals(); document.body.innerHTML = ''; });

	describe('account lifecycle truth', () => {
		it.each(['reopen', 'Check', 'Cancel', 'start'])('reconciles completed %s before rename at the committed revision', async (path) => {
			const fetch = vi.fn().mockResolvedValueOnce(list());
			if (path === 'reopen') fetch.mockResolvedValueOnce(res(completed));
			else {
				fetch.mockResolvedValueOnce(res(missing, 409));
				fetch.mockResolvedValueOnce(res(path === 'start' ? completed : waiting));
				if (path !== 'start') fetch.mockResolvedValueOnce(res(completed));
			}
			fetch.mockResolvedValueOnce(list(inside));
			fetch.mockImplementation(async (_url, init) => {
				const body = JSON.parse(init.body);
				return body.expected_revision === 1 && body.name === 'Renamed' ? res({ ...inside, name: body.name, revision: 2 }) : res({ detail: 'revision changed' }, 409);
			});
			vi.stubGlobal('fetch', fetch);
			const component = mount(AccountsBlock, { target: document.body, props: { provider } });
			await settle();
			if (path !== 'reopen') { button('Sign in').click(); await settle(); }
			if (path === 'Check' || path === 'Cancel') { button(path).click(); await settle(); }
			expect(get(vendorAccounts).find((a) => a.id === 'work')).toMatchObject({ state: 'signed_in', revision: 1 });
			expect(document.querySelector('#vendor-account-work')?.textContent).not.toContain('Signed out');
			expect(document.querySelector('#vendor-account-work .signin-state[role="status"]')).toBeNull();
			expect(document.querySelector('#vendor-account-work .account-meta')).toBeNull();
			await more(); button('Rename').click(); await settle();
			const input = document.querySelector('[aria-label="Rename Work"]') as HTMLInputElement;
			input.value = 'Renamed'; input.dispatchEvent(new Event('input', { bubbles: true })); await settle();
			button('Save').click(); await settle();
			expect(get(vendorAccounts).find((a) => a.id === 'work')).toMatchObject({ name: 'Renamed', revision: 2 });
			unmount(component);
		});

		it.each(['reopen', 'Check', 'Cancel', 'start'])('retains completion and directional reload failure for %s', async (path) => {
			const fetch = vi.fn().mockResolvedValueOnce(list());
			if (path === 'reopen') fetch.mockResolvedValueOnce(res(completed));
			else { fetch.mockResolvedValueOnce(res(missing, 409)).mockResolvedValueOnce(res(path === 'start' ? completed : waiting)); if (path !== 'start') fetch.mockResolvedValueOnce(res(completed)); }
			fetch.mockResolvedValueOnce(res({ detail: 'offline' }, 503)).mockResolvedValueOnce(list(inside));
			vi.stubGlobal('fetch', fetch);
			const component = mount(AccountsBlock, { target: document.body, props: { provider } }); await settle();
			if (path !== 'reopen') { button('Sign in').click(); await settle(); }
			if (path === 'Check' || path === 'Cancel') { button(path).click(); await settle(); }
			expect(document.body.textContent).toContain('Signed in, but the account list could not be reloaded.');
			expect(document.querySelector('#vendor-account-work')?.textContent).not.toContain('Signed out');
			expect(document.querySelector('#vendor-account-work .signin-state[role="status"]')).toBeNull();
			expect(document.querySelector('#vendor-account-work .account-meta')).toBeNull();
			expect(get(vendorAccounts).find((a) => a.id === 'work')).toMatchObject({ state: 'signed_in', revision: 0, details_stale: true });
			await more(); expect(button('Sign out').disabled).toBe(true); expect(button('Rename').disabled).toBe(true); expect(button('Remove').disabled).toBe(true);
			button('Reload').click(); await settle();
			expect(get(vendorAccounts).find((a) => a.id === 'work')).toMatchObject({ state: 'signed_in', revision: 1 });
			unmount(component);
		});

		it('applies confirmed signout and clears completion even when reloading fails', async () => {
			const fetch = vi.fn().mockResolvedValueOnce(list()).mockResolvedValueOnce(res(completed)).mockResolvedValueOnce(list(inside))
				.mockResolvedValueOnce(res({ ...out, revision: 2 })).mockResolvedValueOnce(res({ detail: 'offline' }, 503));
			vi.stubGlobal('fetch', fetch);
			const component = mount(AccountsBlock, { target: document.body, props: { provider } }); await settle(); await more(); button('Sign out').click(); await settle();
			expect(get(vendorAccounts).find((a) => a.id === 'work')).toMatchObject({ state: 'signed_out', revision: 2 });
			expect(document.querySelector('#vendor-account-work')?.textContent).not.toContain('Signed in');
			expect(document.body.textContent).toContain('The account was signed out. The account list could not be reloaded.');
			expect(button('Sign in')).toBeDefined(); unmount(component);
		});

		it('applies confirmed removal while waiting even when reloading fails', async () => {
			const fetch = vi.fn().mockResolvedValueOnce(list()).mockResolvedValueOnce(res(waiting))
				.mockResolvedValueOnce(res({ ...out, state: 'removed', revision: 1 })).mockResolvedValueOnce(res({ detail: 'offline' }, 503));
			vi.stubGlobal('fetch', fetch);
			const component = mount(AccountsBlock, { target: document.body, props: { provider } }); await settle(); await more(); button('Remove').click(); await settle(); button('Remove').click(); await settle();
			expect(get(vendorAccounts).find((a) => a.id === 'work')).toMatchObject({ state: 'removed', revision: 1 });
			expect(document.body.textContent).not.toContain('Waiting for sign-in');
			expect(document.querySelector('#vendor-account-work')).toBeNull();
			expect(document.querySelector('#vendor-account-default')).not.toBeNull();
			expect(document.body.textContent).toContain('The account was removed. The account list could not be reloaded.'); unmount(component);
		});
	});
});

describe('recovery', () => {
	const provider = { name:'codex',label:'Codex',type:'harness' as const,statusDot:'online' as const,subline:'1 model',raw:{name:'codex',label:'Codex',type:'harness' as const,has_key:false,masked_key:null,api_base:null,docs_url:null,models:[]} };
	const ambient = { id:'default',provider:'codex',name:'Default',state:'legacy' as const,is_default:true,revision:0,identity:{},label:{name:'Plus account 1',email:null,text:'Plus account 1'} };
	const work = { id:'work',provider:'codex',name:'Work',state:'signed_out' as const,is_default:false,revision:0,identity:{},label:{name:'Work',email:null,text:'Work'} };
	const other = { ...work,id:'other',name:'Other',label:{name:'Other',email:null,text:'Other'} };
	const receipt = (id:string,status:string,extra={}) => ({account_id:id,status,cause:null,route:'browser',...extra});
	const res = (data:unknown,status=200) => new Response(JSON.stringify(data),{status});
	const list = (rows:VendorAccount[]=[ambient,work]) => res({accounts:rows});
	const row = (id='work') => document.querySelector(`#vendor-account-${id}`)!;
	const button = (name:string,scope:Element|Document=document) => [...scope.querySelectorAll<HTMLButtonElement>('button')].find(b=>b.textContent?.trim()===name)!;
	const key = (input:HTMLInputElement,k:string) => input.dispatchEvent(new KeyboardEvent('keydown',{key:k,bubbles:true}));
	async function settle(){await new Promise(r=>setTimeout(r,0));flushSync();}
	async function edit(id='work'){row(id).dispatchEvent(new MouseEvent('contextmenu',{bubbles:true,cancelable:true,clientX:8,clientY:8}));await settle();button('Rename').click();await settle();}
	function type(text:string,scope:Element|Document=document){const input=scope.querySelector<HTMLInputElement>('input')!;input.value=text;input.dispatchEvent(new Event('input',{bubbles:true}));return input;}
	function deferred(){let resolve!:(r:Response)=>void;const promise=new Promise<Response>(r=>resolve=r);return {promise,resolve};}
	let clearBoundary = () => {};
	afterEach(()=>{clearBoundary();vendorAccounts.set([]);vi.unstubAllGlobals();document.body.innerHTML='';});

	it('blocks stale revision writes after confirmed completion without fabricating revision',async()=>{
	 const fetch=vi.fn().mockResolvedValueOnce(res(receipt('work','completed'))).mockResolvedValueOnce(res({detail:'offline'},503));vi.stubGlobal('fetch',fetch);vendorAccounts.set([ambient,work]);
	 await checkVendorSignin('work');const confirmed=get(vendorAccounts).find(a=>a.id==='work')!;
	 expect(confirmed).toMatchObject({state:'signed_in',revision:0,details_stale:true});
	 expect((await renameVendorAccount(work,'New')).ok).toBe(false);expect((await signOutVendorAccount(work)).ok).toBe(false);expect((await removeVendorAccount(work)).ok).toBe(false);expect(fetch).toHaveBeenCalledTimes(2);
	});
	it('keeps pending Add input through Escape and Cancel',async()=>{
	 const held=deferred();const fetch=vi.fn().mockResolvedValueOnce(list([ambient])).mockReturnValue(held.promise);vi.stubGlobal('fetch',fetch);
	 const host=document.createElement('div');document.body.append(host);const bubbled=vi.fn();window.addEventListener('keydown',bubbled);clearBoundary=()=>window.removeEventListener('keydown',bubbled);
	 const component=mount(AccountsBlock,{target:host,props:{provider}});await settle();button('Add account').click();await settle();const input=type('Work');await settle();key(input,'Enter');await settle();key(input,'Escape');button('Cancel').click();button('Add account').click();await settle();
	 expect(document.querySelector('input')).toBe(input);expect(input.value).toBe('Work');expect(bubbled).not.toHaveBeenCalled();expect(button('Cancel').disabled).toBe(true);expect(fetch.mock.calls.filter(([,init])=>init?.method==='POST')).toHaveLength(1);unmount(component);
	});
	it('guards repeated Save and prevents pending rename dismissal or replacement',async()=>{
	 const held=deferred();const fetch=vi.fn().mockResolvedValueOnce(list([ambient,work,other])).mockResolvedValueOnce(res(receipt('work','cancelled'))).mockResolvedValueOnce(res(receipt('other','cancelled'))).mockReturnValue(held.promise);vi.stubGlobal('fetch',fetch);
	 const host=document.createElement('div');document.body.append(host);const bubbled=vi.fn();window.addEventListener('keydown',bubbled);clearBoundary=()=>window.removeEventListener('keydown',bubbled);
	 const component=mount(AccountsBlock,{target:host,props:{provider}});await settle();await edit();const input=type('Typed',row());await settle();key(input,'Enter');key(input,'Enter');await settle();key(input,'Escape');button('Cancel',row()).click();await edit('other');await settle();
	 expect(bubbled).not.toHaveBeenCalled();expect(row().querySelector('input')).toBe(input);expect(row('other').querySelector('input')).toBeNull();expect(button('Cancel',row()).disabled).toBe(true);expect(fetch.mock.calls.filter(([,init])=>init?.method==='PATCH')).toHaveLength(1);unmount(component);
	});
	it('keeps each overlapping account request pending when another finishes',async()=>{
	 const workHeld=deferred(),otherHeld=deferred();const fetch=vi.fn().mockResolvedValueOnce(list([ambient,work,other])).mockResolvedValueOnce(res(receipt('work','waiting'))).mockResolvedValueOnce(res(receipt('other','waiting'))).mockReturnValueOnce(workHeld.promise).mockReturnValueOnce(otherHeld.promise);vi.stubGlobal('fetch',fetch);
	 const component=mount(AccountsBlock,{target:document.body,props:{provider}});await settle();button('Check',row()).click();button('Check',row('other')).click();await settle();workHeld.resolve(res(receipt('work','waiting')));await settle();
	 expect(button('Check',row()).disabled).toBe(false);expect(button('Check',row('other')).disabled).toBe(true);expect(button('Cancel',row('other')).disabled).toBe(true);button('Check',row('other')).click();expect(fetch).toHaveBeenCalledTimes(5);otherHeld.resolve(res(receipt('other','waiting')));await settle();unmount(component);
	});
	it('does not retain a present-tense holder claim after its completion',async()=>{
	 const fetch=vi.fn().mockResolvedValueOnce(list([ambient,work,other])).mockResolvedValueOnce(res(receipt('work','waiting'))).mockResolvedValueOnce(res(receipt('other','refused',{held_by:'work',cause:'vendor_busy'}))).mockResolvedValueOnce(res(receipt('work','completed'))).mockResolvedValueOnce(res({detail:'offline'},503));vi.stubGlobal('fetch',fetch);
	 const component=mount(AccountsBlock,{target:document.body,props:{provider}});await settle();expect(row('other').textContent).toContain('Work is already signing in');button('Check',row()).click();await settle();expect(row('other').textContent).not.toContain('Work is already signing in');expect(row('other').textContent).toContain('Sign-in was already in progress for Work');expect(button('Sign in',row('other'))).toBeDefined();unmount(component);
	});
	it.each([false,true])('reloads rename conflict in place preserving text; failed first reload=%s',async(failFirst)=>{
	 const inside={...work,state:'signed_in' as const,revision:2};const fetch=vi.fn().mockResolvedValueOnce(list([ambient,inside])).mockResolvedValueOnce(res({detail:'account revision changed; refresh before acting'},409));if(failFirst)fetch.mockResolvedValueOnce(res({detail:'offline'},503));fetch.mockResolvedValueOnce(list([ambient,{...inside,revision:3}])).mockResolvedValueOnce(res({...inside,name:'Typed',revision:4}));vi.stubGlobal('fetch',fetch);
	 const component=mount(AccountsBlock,{target:document.body,props:{provider}});await settle();await edit();const input=type('Typed',row());await settle();button('Save',row()).click();await settle();expect(button('Save',row()).disabled).toBe(true);key(input,'Enter');await settle();expect(fetch.mock.calls.filter(([,init])=>init?.method==='PATCH')).toHaveLength(1);
	 if(failFirst){button('Reload',row()).click();await settle();expect(row().textContent).toContain('offline');expect(input.value).toBe('Typed');expect(button('Save',row()).disabled).toBe(true);key(input,'Enter');expect(fetch.mock.calls.filter(([,init])=>init?.method==='PATCH')).toHaveLength(1);}
	 button('Reload',row()).click();await settle();expect(input.value).toBe('Typed');expect(button('Save',row()).disabled).toBe(false);expect(fetch.mock.calls.filter(([,init])=>init?.method==='PATCH')).toHaveLength(1);button('Save',row()).click();await settle();expect(JSON.parse(fetch.mock.calls.at(-1)![1].body)).toEqual({name:'Typed',expected_revision:3});expect(get(vendorAccounts).find(a=>a.id==='work')).toMatchObject({name:'Typed',revision:4});unmount(component);
	});
});

describe('sign-in submit', () => {
	const provider = { name: 'codex', label: 'Codex', type: 'harness' as const, statusDot: 'online' as const, subline: '1 model', raw: { name: 'codex', label: 'Codex', type: 'harness' as const, has_key: false, masked_key: null, api_base: null, docs_url: null, models: [] } };
	const ambient = { id: 'default', provider: 'codex', name: 'Default', state: 'legacy', is_default: true, revision: 0, identity: {}, label: { name: 'Plus account 1', email: null, text: 'Plus account 1' } };
	const work = { id: 'work', provider: 'codex', name: 'Work', state: 'signed_out', is_default: false, revision: 0, identity: {}, label: { name: 'Work', email: null, text: 'Work' } };
	const response = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
	const button = (name: string) => [...document.querySelectorAll<HTMLButtonElement>('button')].find(b => b.textContent?.trim() === name)!;
	async function settle() { await new Promise(r => setTimeout(r, 0)); flushSync(); }
	afterEach(() => { vendorAccounts.set([]); vi.unstubAllGlobals(); document.body.innerHTML = ''; });
	it('does not label a confirmed completed sign-in as signed out when refresh fails', async () => {
	 const fetch = vi.fn().mockResolvedValueOnce(response({ accounts: [ambient, work] })).mockResolvedValueOnce(response({ account_id: 'work', status: 'completed', cause: null, route: 'browser' })).mockResolvedValueOnce(response({ detail: 'offline' }, 503));
	 vi.stubGlobal('fetch', fetch);
	 const component = mount(AccountsBlock, { target: document.body, props: { provider } });
	 await settle(); await settle();
	 expect(document.body.textContent).toContain('Signed in, but the account list could not be reloaded.');
	 expect(document.querySelector('#vendor-account-work')?.textContent).not.toContain('Signed out');
	 unmount(component);
	});
	it('submits Add only once while the first keyboard request is pending', async () => {
	 const fetch = vi.fn().mockResolvedValueOnce(response({ accounts: [ambient] })).mockImplementation(() => new Promise(() => {}));
	 vi.stubGlobal('fetch', fetch);
	 const component = mount(AccountsBlock, { target: document.body, props: { provider } });
	 await settle(); button('Add account').click(); await settle();
	 const input = document.querySelector('input')!;
	 input.value = 'Work'; input.dispatchEvent(new Event('input', { bubbles: true })); await settle();
	 input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
	 input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true })); await settle();
	 expect(fetch.mock.calls.filter(([, init]) => init?.method === 'POST')).toHaveLength(1);
	 unmount(component);
	});
});

describe('billing in the account row', () => {
	const provider = { name: 'codex', label: 'Codex', type: 'harness' as const, statusDot: 'online' as const, subline: '1 model', raw: { name: 'codex', label: 'Codex', type: 'harness' as const, has_key: false, masked_key: null, api_base: null, docs_url: null, models: [] } };
	const row = (over: Record<string, unknown> = {}) => ({ id: 'work', provider: 'codex', name: 'Work', state: 'signed_in', is_default: false, revision: 2, identity: {}, label: { name: 'Work', email: null, text: 'Work', custom: null }, billing: null, billing_revision: 1, ...over });
	const reply = (data: unknown, status = 200) => new Response(JSON.stringify(data), { status });
	const button = (name: string) => [...document.querySelectorAll<HTMLButtonElement>('button')].find((item) => item.textContent?.trim() === name)!;
	async function settle() { await new Promise((resolve) => setTimeout(resolve, 0)); flushSync(); }
	afterEach(() => { vendorAccounts.set([]); vi.unstubAllGlobals(); document.body.innerHTML = ''; });

	it('shows the billing row under the overage switch and saves into the same row, with no Billing history entry', async () => {
		const saved = row({ billing: { monthly_price_cents: 2000, charge_day: 14 }, billing_revision: 2 });
		vi.stubGlobal('fetch', vi.fn().mockResolvedValueOnce(reply({ accounts: [row()] })).mockResolvedValueOnce(reply(saved)));
		const component = mount(AccountsBlock, { target: document.body, props: { provider } });
		await settle();
		expect(document.body.textContent).toContain('Billing Price not recorded');
		expect(document.body.textContent).not.toContain('Billing history');
		(document.querySelector<HTMLElement>('#vendor-account-work')!).dispatchEvent(new MouseEvent('contextmenu', { bubbles: true, cancelable: true, clientX: 8, clientY: 8 })); await settle();
		expect(document.body.textContent).not.toContain('Billing history');
		button('Set price').click(); flushSync();
		const price = document.querySelector('input[inputmode="decimal"]') as HTMLInputElement;
		price.value = '20'; price.dispatchEvent(new Event('input', { bubbles: true }));
		const day = document.querySelector('select') as HTMLSelectElement;
		day.value = '14'; day.dispatchEvent(new Event('change', { bubbles: true }));
		flushSync();
		button('Save').click(); await settle();
		expect(document.body.textContent).toContain('Billing $20.00 a month · charged on the 14th');
		unmount(component);
	});
});
