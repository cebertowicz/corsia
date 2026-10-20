import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ConnectedAppsSettings from './ConnectedAppsSettings.svelte';
import WhereItWorks from './WhereItWorks.svelte';
import { token } from '$lib/stores/auth';
import { projects } from '$lib/stores/projects';
import { get } from 'svelte/store';
import { resetSettingsNavigation, settingsNavigation } from '$lib/stores/settingsNavigation';

const awaiting = { id: 'github', name: 'GitHub', state: 'awaiting_approval', revision: 1, reach: { mode: 'all', project_ids: [], outside_projects: false } };
const googleAccount = { id: 'google-work', provider: 'google' as const, address: 'sam.work@example.com', capabilities: ['mail', 'calendar', 'files'] as ('mail' | 'calendar' | 'files')[], state: 'connected' as const, reach: { mode: 'corsia' as const, project_ids: [], outside_projects: false } };
let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	vi.spyOn(HTMLCanvasElement.prototype, 'getContext').mockReturnValue({ measureText: (text: string) => ({ width: text.length * 8 }) } as CanvasRenderingContext2D);
	token.set('test-token');
	projects.set([{ id: 'q4', name: 'Q4 launch', status: 'active', accent_color: null }]);
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	token.set(null);
});

async function settle(): Promise<void> { await Promise.resolve(); await Promise.resolve(); flushSync(); }

describe('ConnectedAppsSettings', () => {
	it('renders the approval row and calls approve', async () => {
		const fetch = vi.fn(async (input: RequestInfo | URL) => ({ ok: true, json: async () => String(input).includes('/approve') ? { ...awaiting, state: 'working', tool_count: 2 } : { servers: [awaiting] } }));
		vi.stubGlobal('fetch', fetch);
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('Waiting for your approval'));
		([...host.querySelectorAll('button')].find((button) => button.textContent?.includes('Approve')) as HTMLButtonElement).click();
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/mcp/servers/github/approve'), expect.objectContaining({ method: 'POST', body: JSON.stringify({ expected_revision: 1 }) })));
		await vi.waitFor(() => expect(host.textContent).toContain('Connected · 2 actions'));
		const connectedRow = host.querySelector('.item');
		expect(connectedRow?.querySelector('.face.open')?.getAttribute('aria-expanded')).toBe('false');
		expect(connectedRow?.querySelector('.chev')).not.toBeNull();
	});

	it('uses the waiting-row type treatment for connected rows too', async () => {
		const connected = { ...awaiting, id: 'notes', name: 'Notes', state: 'working', tool_count: 2 };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [connected, awaiting] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelectorAll('[data-settings-row] .name')).toHaveLength(2));
		expect(host.querySelector('.item .sub')?.classList.contains('attention')).toBe(false);
		expect(host.querySelector('.item .sub')?.textContent).toBe('Connected · 2 actions · All projects');
	});

	it('uses amber only for connected-app states needing attention', async () => {
		const needsSignIn = { ...awaiting, id: 'notion', name: 'Notion', state: 'needs_sign_in' };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [needsSignIn] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelector('.sub.attention')?.textContent).toContain('Needs you to sign in'));
	});

	it('calls decline for an awaiting approval row', async () => {
		const fetch = vi.fn(async (input: RequestInfo | URL) => ({ ok: true, json: async () => String(input).includes('/decline') ? { ...awaiting, state: 'expired' } : { servers: [awaiting] } }));
		vi.stubGlobal('fetch', fetch);
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('Decline'));
		([...host.querySelectorAll('button')].find((button) => button.textContent?.includes('Decline')) as HTMLButtonElement).click();
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/mcp/servers/github/decline'), { method: 'POST' }));
	});

	it('removes the row once Decline deletes a new request, and keeps the others', async () => {
		const second = { ...awaiting, id: 'linear', name: 'Linear' };
		const fetch = vi.fn(async (input: RequestInfo | URL) => ({ ok: true, json: async () => String(input).includes('/decline') ? { id: 'github', removed: true } : { servers: [awaiting, second] } }));
		vi.stubGlobal('fetch', fetch);
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelectorAll('.item').length).toBe(2));
		([...host.querySelectorAll('.item')[0].querySelectorAll('button')].find((button) => button.textContent?.includes('Decline')) as HTMLButtonElement).click();
		await vi.waitFor(() => expect(host.querySelectorAll('.item').length).toBe(1));
		expect(host.querySelector('[data-settings-row] .name')?.textContent).toBe('Linear');
	});

	it('names the projects an app works in on its row', async () => {
		const catalog = [{ id: 'q4', name: 'Q4 launch', status: 'active', accent_color: null }, { id: 'home', name: 'Home renovation', status: 'active', accent_color: null }];
		const selected = { ...awaiting, state: 'working', reach: { mode: 'selected', project_ids: ['q4', 'home'], outside_projects: true } };
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => ({ ok: true, json: async () => String(input).includes('/api/projects') ? { projects: catalog } : { servers: [selected] } })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('Q4 launch, Home renovation, 1 more'));
		expect(host.textContent).not.toContain('Select individually');
	});

	it('shows an empty-reach row as waiting for a place, with no Approve until one is saved', async () => {
		const folderApp = { ...awaiting, id: 'postgres', name: 'Postgres', reach: { mode: 'selected', project_ids: [], outside_projects: false } };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [folderApp] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('Waiting for your approval'));
		expect(host.querySelector('[data-settings-row] .sub.attention')?.textContent).toBe('Waiting for your approval');
		const labels = [...host.querySelectorAll('.settings-row .actions button')].map((button) => button.textContent?.trim());
		expect(labels).toEqual(['Choose where it works…', 'Decline']);
	});

	it('shows the folder label as plain text under the status', async () => {
		const folderApp = { ...awaiting, id: 'postgres', name: 'Postgres', folder: '<b>shop</b>', reach: { mode: 'selected', project_ids: [], outside_projects: false } };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [folderApp] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('From Claude Code · <b>shop</b> folder'));
		expect(host.querySelector('[data-settings-row] b')).toBeNull();
	});

	it('gives every waiting row the same rhythm: an idle sign-in view reserves no empty panel', async () => {
		const place = { mode: 'selected', project_ids: [], outside_projects: false };
		const rows = [
			{ ...awaiting, id: 'acme', name: 'Acme', reach: place },
			{ ...awaiting, id: 'notes', name: 'Notes', transport: 'http', sign_in: { status: 'idle', signed_in: false }, reach: place },
			{ ...awaiting, id: 'postgres', name: 'Postgres', reach: place },
		];
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: rows }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelectorAll('.item').length).toBe(3));
		for (const row of host.querySelectorAll('.item')) {
			expect(row.querySelector('.details')).toBeNull();
			expect([...row.children].map((child) => child.classList.contains('settings-row') ? 'row' : 'other')).toEqual(['row']);
		}
	});

	it('still shows the sign-in panel on a waiting row once there is something to say', async () => {
		const pending = { ...awaiting, id: 'notes', name: 'Notes', transport: 'http', sign_in: { status: 'pending', signed_in: false }, reach: { mode: 'selected', project_ids: [], outside_projects: false } };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [pending] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelector('.details')).not.toBeNull());
	});

	it('lists the rows in the order the server sends them', async () => {
		const place = { mode: 'selected', project_ids: [], outside_projects: false };
		const rows = ['Acme', 'Linear', 'Notes', 'Postgres'].map((name) => ({ ...awaiting, id: name.toLowerCase(), name, reach: place }));
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: rows }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelectorAll('.item').length).toBe(4));
		expect([...host.querySelectorAll('[data-settings-row] .name')].map((el) => el.textContent)).toEqual(['Acme', 'Linear', 'Notes', 'Postgres']);
	});

	it('offers only Dismiss on an expired request', async () => {
		const expired = { ...awaiting, state: 'expired', reach: { mode: 'selected', project_ids: [], outside_projects: false } };
		const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({ ok: true, json: async () => ({ servers: [expired] }) }));
		vi.stubGlobal('fetch', fetch);
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('Request expired'));
		expect(host.textContent).not.toContain('Choose where it works…');
		expect(host.textContent).not.toContain('Reconnect');
		(host.querySelector('.item button.open') as HTMLButtonElement).click();
		flushSync();
		expect([...host.querySelectorAll('.item .details .actions button')].map((button) => button.textContent?.trim())).toEqual(['Dismiss']);
		(host.querySelector('.item .details .actions button') as HTMLButtonElement).click();
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/mcp/servers/github/decline'), { method: 'POST' }));
	});

	it('shows a pending change on a working app as Approve and Decline, with no place action', async () => {
		const pending = { ...awaiting, tool_count: 3, reach: { mode: 'selected', project_ids: ['q4'], outside_projects: false } };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [pending] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('Waiting for your approval'));
		const labels = [...host.querySelectorAll('.settings-row .actions button')].map((button) => button.textContent?.trim());
		expect(labels).toEqual(['Approve', 'Decline']);
		expect(host.textContent).not.toContain('Choose where it works…');
	});

	it('shows a failed action on its own row only', async () => {
		const second = { ...awaiting, id: 'linear', name: 'Linear' };
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input).includes('/decline')
			? { ok: false, status: 500, json: async () => ({ detail: 'nope' }), text: async () => 'nope' }
			: { ok: true, json: async () => ({ servers: [awaiting, second] }) }));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelectorAll('.item').length).toBe(2));
		([...host.querySelectorAll('.item')[1].querySelectorAll('button')].find((b) => b.textContent?.includes('Decline')) as HTMLButtonElement).click();
		await vi.waitFor(() => expect(host.querySelectorAll('.item .error').length).toBe(1));
		expect(host.querySelectorAll('.item')[1].querySelector('.error')).not.toBeNull();
		expect(host.querySelectorAll('.item')[0].querySelector('.error')).toBeNull();
	});

	it('approves an empty-reach row from the sheet with the chosen place and the row revision', async () => {
		const folderApp = { ...awaiting, id: 'postgres', name: 'Postgres', revision: 3, reach: { mode: 'selected', project_ids: [], outside_projects: false } };
		const catalog = [{ id: 'q4', name: 'Q4 launch', status: 'active', accent_color: null }];
		const fetch = vi.fn(async (input: RequestInfo | URL) => ({ ok: true, json: async () => String(input).includes('/approve')
			? { ...folderApp, state: 'working', revision: 4, reach: { mode: 'selected', project_ids: ['q4'], outside_projects: false } }
			: String(input).includes('/api/projects') ? { projects: catalog } : { servers: [folderApp] } }));
		vi.stubGlobal('fetch', fetch);
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('Choose where it works…'));
		([...host.querySelectorAll('button')].find((button) => button.textContent?.includes('Choose where it works…')) as HTMLButtonElement).click();
		flushSync();
		const approve = () => [...document.body.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Approve') as HTMLButtonElement;
		expect(approve().disabled).toBe(true);
		([...document.body.querySelectorAll('input[type="checkbox"]')].find((input) => (input.parentElement?.textContent ?? '').includes('Q4 launch')) as HTMLInputElement).click();
		flushSync();
		expect(document.body.textContent).toContain('Agents in Q4 launch will be able to use Postgres.');
		expect(approve().disabled).toBe(false);
		approve().click();
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/mcp/servers/postgres/approve'), expect.objectContaining({
			method: 'POST',
			body: JSON.stringify({ expected_revision: 3, reach: { mode: 'selected', project_ids: ['q4'], outside_projects: false } }),
		})));
		await vi.waitFor(() => expect(host.textContent).toContain('Connected'));
	});

	it("shows the server's Mac notice on a waiting row, so a direct Approve shows it too", async () => {
		const local = { ...awaiting, transport: 'stdio', notice: 'notice text from the server' };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [local, { ...awaiting, id: 'web', name: 'Web' }] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelectorAll('.item')).toHaveLength(2));
		const [first, second] = host.querySelectorAll('.item');
		expect(first.textContent).toContain('notice text from the server');
		expect(second.textContent).not.toContain('notice text from the server');
	});

	describe('approving an app that needs a sign-in', () => {
		const tracker = { ...awaiting, id: 'tracker', name: 'Tracker', transport: 'http', sign_in: { status: 'idle', signed_in: false } };
		const signInPosts = (fetch: ReturnType<typeof vi.fn>) => fetch.mock.calls.filter(([input, init]) => String(input).includes('/sign-in') && (init as RequestInit | undefined)?.method === 'POST');
		function serve(row: typeof tracker, approvedState: string) {
			return vi.fn(async (input: RequestInfo | URL) => ({ ok: true, status: 200, clone() { return this; }, json: async () => {
				const path = String(input);
				if (path.includes('/approve')) return { ...row, state: approvedState, revision: 2 };
				if (path.includes('/sign-in')) return { authorization_url: 'https://auth.example.test/authorize?state=abc', expires_at: 't', reopened: false };
				if (path.includes('/api/projects')) return { projects: [{ id: 'q4', name: 'Q4 launch', status: 'active', accent_color: null }] };
				return { servers: [row], sign_in_here: true };
			} }));
		}

		it('starts the sign-in once from the Approve click, with no second click', async () => {
			vi.stubGlobal('open', vi.fn(() => null));
			const fetch = serve(tracker, 'needs_sign_in');
			vi.stubGlobal('fetch', fetch);
			component = mount(ConnectedAppsSettings, { target: host });
			await vi.waitFor(() => expect(host.textContent).toContain('Waiting for your approval'));
			([...host.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Approve') as HTMLButtonElement).click();
			await vi.waitFor(() => expect(signInPosts(fetch)).toHaveLength(1));
			await vi.waitFor(() => expect(host.textContent).toContain("Tracker's sign-in page is open in your browser."));
			await settle();
			expect(signInPosts(fetch)).toHaveLength(1);
		});

		it('starts the sign-in once from the place sheet that approves', async () => {
			vi.stubGlobal('open', vi.fn(() => null));
			const placeless = { ...tracker, reach: { mode: 'selected' as const, project_ids: [], outside_projects: false } };
			const fetch = serve(placeless, 'needs_sign_in');
			vi.stubGlobal('fetch', fetch);
			component = mount(ConnectedAppsSettings, { target: host });
			await vi.waitFor(() => expect(host.textContent).toContain('Choose where it works…'));
			([...host.querySelectorAll('button')].find((button) => button.textContent?.includes('Choose where it works…')) as HTMLButtonElement).click();
			flushSync();
			([...document.body.querySelectorAll('input[type="checkbox"]')].find((input) => (input.parentElement?.textContent ?? '').includes('Q4 launch')) as HTMLInputElement).click();
			flushSync();
			([...document.body.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Approve') as HTMLButtonElement).click();
			await vi.waitFor(() => expect(signInPosts(fetch)).toHaveLength(1));
			await settle();
			expect(signInPosts(fetch)).toHaveLength(1);
		});

		it('starts no sign-in when the approved app connects without one', async () => {
			const fetch = serve(tracker, 'working');
			vi.stubGlobal('fetch', fetch);
			component = mount(ConnectedAppsSettings, { target: host });
			await vi.waitFor(() => expect(host.textContent).toContain('Waiting for your approval'));
			([...host.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Approve') as HTMLButtonElement).click();
			await vi.waitFor(() => expect(host.textContent).toContain('Connected'));
			await settle();
			expect(signInPosts(fetch)).toHaveLength(0);
		});
	});

	it('renders the empty-state copy when there are no apps', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('No connected apps yet. Connect one here, or ask CORSIA in chat to bring over the apps you use in Claude Code, Claude Desktop, Cursor, Codex, OpenClaw or Hermes Agent'));
		expect(host.textContent).not.toContain('No apps shared with every project yet.');
	});

	it('draws the state in its colour: red for a failure, amber for waiting on the person, quiet when working', async () => {
		const failed = { ...awaiting, id: 'slack', name: 'Slack', state: 'failed', failure_class: 'cannot_start' };
		const working = { ...awaiting, id: 'notes', name: 'Notes', state: 'working' };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [failed, awaiting, working] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelectorAll('[data-settings-row]').length).toBe(3));
		const tone = (name: string) => host.querySelector(`[data-settings-row="${name}"] .sub`)?.className ?? '';
		expect(tone('Slack')).toContain('failed');
		expect(tone('GitHub')).toContain('attention');
		expect(tone('Notes')).not.toMatch(/failed|attention/);
		expect(host.querySelector('[data-settings-row="Slack"] .sub')?.textContent).toBe("Couldn't connect: app didn't start · All projects");
	});

	it('shows the runtime-download failure and keeps Reconnect as its recovery', async () => {
		const failed = { ...awaiting, id: 'files', name: 'Files', state: 'failed', failure_class: 'runtime_download_failed' };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [failed] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain("Couldn't connect: download failed"));
		(host.querySelector('[data-settings-row="Files"] button.open') as HTMLButtonElement).click();
		flushSync();
		expect(host.textContent).toContain("CORSIA couldn't download what this app needs to run on this computer. Check that you're connected to the internet, then press Reconnect. If it keeps happening, your network may be blocking the download.");
		expect(host.textContent).toContain('Reconnect');
	});

	it('gives a row at most one solid action and keeps the others as text', async () => {
		const connected = { ...awaiting, id: 'notes', name: 'Notes', state: 'working' };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [awaiting, connected] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelectorAll('[data-settings-row]').length).toBe(2));
		(host.querySelector('[data-settings-row="Notes"] button.open') as HTMLButtonElement).click();
		flushSync();
		expect(host.querySelectorAll('[data-settings-row="GitHub"] .actions .btn.primary')).toHaveLength(1);
		expect(host.querySelectorAll('.item .btn.secondary, .item .btn.danger')).toHaveLength(0);
		expect(host.querySelector('[data-settings-row="Notes"] button.open')?.getAttribute('aria-expanded')).toBe('true');
		expect([...host.querySelectorAll('.item .details .actions .btn.ghost')].map((b) => b.textContent?.trim())).toEqual(['Where it works…', 'Reconnect', 'Remove']);
	});

	it('keeps the Connect an app action with its introductory copy', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('No connected apps yet.'));
		expect(host.querySelector('h2')).toBeNull();
		expect(host.querySelector('.heading .btn')?.textContent?.trim()).toBe('Connect an app');
		expect(host.querySelector('p')?.textContent).toContain('Connect the apps you already use');
	});

	it('renders Google accounts before hosted apps with their own state, reach and capabilities', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [{ ...awaiting, state: 'working' }], accounts: [googleAccount, { ...googleAccount, id: 'google-personal', address: 'sam@example.com' }] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Google — sam.work@example.com"]')).not.toBeNull());
		const names = [...host.querySelectorAll<HTMLElement>('[data-settings-row]')].map((row) => row.querySelector<HTMLButtonElement>('button')?.ariaLabel ?? row.querySelector('.name')?.textContent);
		expect(names).toEqual(['Google — sam.work@example.com', 'Google — sam@example.com', 'GitHub']);
		expect(host.textContent).toContain('Connected · Only CORSIA');
		(host.querySelector('[data-connected-account-id="google-work"] button') as HTMLButtonElement).click();
		flushSync();
		expect(host.textContent).toContain('ReachesEmail, Calendar, Files');
		expect(host.querySelector('.capability-label')?.textContent).toBe('Reaches');
	});

	it('keeps same-prefix Google account titles distinct while preserving their domains', async () => {
		const accounts = [
			{ ...googleAccount, id: 'google-work', address: 'shared.prefix.very-long-work-address@example.com' },
			{ ...googleAccount, id: 'google-personal', address: 'shared.prefix.very-long-personal-address@example.com' },
			{ ...googleAccount, id: 'google-alpha', address: 'alexandria.long-project-alpha-address@example.com' },
			{ ...googleAccount, id: 'google-beta', address: 'alexandria.long-project-beta-address@example.com' },
		];
		vi.spyOn(HTMLElement.prototype, 'clientWidth', 'get').mockImplementation(function(this: HTMLElement) { return this.classList.contains('account-closed-name') ? 296 : 0; });
		vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function(this: HTMLElement) {
			const width = this.classList.contains('account-provider') ? 56 : this.classList.contains('account-local-suffix') ? 64 : this.classList.contains('account-domain') ? 96 : this.classList.contains('account-closed-name') ? 296 : 0;
			return { width, right: width } as DOMRect;
		});
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [], accounts }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelectorAll('.account-local-prefix')).toHaveLength(4));
		const prefixes = [...host.querySelectorAll('.account-local-prefix')].map((node) => node.textContent);
		const titles = [...host.querySelectorAll('.account-closed-name')].map((node) => node.textContent);
		expect(new Set(prefixes).size).toBe(4);
		expect(prefixes.join(' ')).toMatch(/alpha.*beta/);
		expect(titles.every((title) => title?.endsWith('@example.com'))).toBe(true);
	});

	it('keeps account rows visible with the approved offline message after a failed refresh', async () => {
		let calls = 0;
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			if (String(input).includes('/api/projects')) return { ok: true, json: async () => ({ projects: [] }) };
			calls += 1;
			if (calls === 1) return { ok: true, json: async () => ({ servers: [], accounts: [googleAccount] }) };
			throw new Error('network failed');
		}));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Google — sam.work@example.com"]')).not.toBeNull());
		window.dispatchEvent(new Event('focus'));
		await vi.waitFor(() => expect(host.textContent).toContain('Showing last known items.'));
		expect(host.querySelector('[aria-label="Google — sam.work@example.com"]')).not.toBeNull();
		(host.querySelector('[data-connected-account-id="google-work"] button') as HTMLButtonElement).click();
		flushSync();
		expect(host.textContent).toContain('Actions are unavailable while offline.');
		for (const label of ['Where it works…', 'Sign out', 'Remove']) expect([...host.querySelectorAll<HTMLButtonElement>('button')].find((button) => button.textContent?.trim() === label)?.disabled).toBe(true);
		expect(host.textContent).not.toContain('network failed');
	});

	it('shows the manual Google Account step only when account sign-out reports a provider failure', async () => {
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
			const path = String(input);
			if (path.includes('/api/projects')) return { ok: true, json: async () => ({ projects: [] }) };
			if (path.includes('/sign-out')) return { ok: true, json: async () => ({ row: { ...googleAccount, state: 'signed_out' }, revocation: 'provider_timeout' }) };
			return { ok: true, json: async () => ({ servers: [], accounts: [googleAccount] }) };
		}));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelector('[aria-label="Google — sam.work@example.com"]')).not.toBeNull());
		(host.querySelector('[data-connected-account-id="google-work"] button') as HTMLButtonElement).click();
		flushSync();
		([...host.querySelectorAll('button')].find((button) => button.textContent?.trim() === 'Sign out') as HTMLButtonElement).click();
		flushSync();
		([...document.body.querySelectorAll('button')].filter((button) => button.textContent?.trim() === 'Sign out').at(-1) as HTMLButtonElement).click();
		await vi.waitFor(() => expect(host.textContent).toContain('Google Account'));
	});
});

describe('WhereItWorks', () => {
	it('shows the approved widening line for All projects', async () => {
		projects.set([{ id: 'q4', name: 'Q4 launch', status: 'active', accent_color: null }, { id: 'home', name: 'Home renovation', status: 'active', accent_color: null }]);
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ projects: [{ id: 'q4', name: 'Q4 launch', status: 'active', accent_color: null }, { id: 'home', name: 'Home renovation', status: 'active', accent_color: null }] }) })));
		component = mount(WhereItWorks, { target: host, props: { app: awaiting, onClose: vi.fn(), onSaved: vi.fn() } });
		await settle();
		expect(host.textContent).toContain('Agents in Q4 launch, Home renovation, Outside projects and any new project will be able to use GitHub.');
	});

	it('shows the approved narrowing line for a move between selected projects', async () => {
		projects.set([{ id: 'garden', name: 'Garden plan', status: 'active', accent_color: null }, { id: 'q4', name: 'Q4 launch', status: 'active', accent_color: null }]);
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ projects: [{ id: 'garden', name: 'Garden plan', status: 'active', accent_color: null }, { id: 'q4', name: 'Q4 launch', status: 'active', accent_color: null }] }) })));
		component = mount(WhereItWorks, { target: host, props: { app: { ...awaiting, reach: { mode: 'selected', project_ids: ['garden'], outside_projects: false } }, onClose: vi.fn(), onSaved: vi.fn() } });
		await settle();
		([...host.querySelectorAll('input[type="checkbox"]')].find((input) => (input.parentElement?.textContent ?? '').includes('Garden plan')) as HTMLInputElement).click();
		([...host.querySelectorAll('input[type="checkbox"]')].find((input) => (input.parentElement?.textContent ?? '').includes('Q4 launch')) as HTMLInputElement).click();
		flushSync();
		expect(host.textContent).toContain("GitHub will move from Garden plan to Q4 launch, with its sign-in and key. Agents in Garden plan won't be able to use it.");
	});

	it('uses account-specific lines when widening and moving a Google account', async () => {
		projects.set([{ id: 'garden', name: 'Garden plan', status: 'active', accent_color: null }, { id: 'q4', name: 'Q4 launch', status: 'active', accent_color: null }]);
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ projects: [{ id: 'garden', name: 'Garden plan', status: 'active', accent_color: null }, { id: 'q4', name: 'Q4 launch', status: 'active', accent_color: null }] }) })));
		component = mount(WhereItWorks, { target: host, props: { app: { ...googleAccount, kind: 'account' as const }, onClose: vi.fn(), onSaved: vi.fn() } });
		await settle();
		(host.querySelector('input[type="checkbox"]') as HTMLInputElement).click();
		flushSync();
		// The CORSIA agent works outside projects: without Outside projects the account leaves it.
		expect(host.textContent).toContain("Google — sam.work@example.com will move from CORSIA to Garden plan, with its sign-in. CORSIA won't be able to use it.");
		([...host.querySelectorAll('input[type="checkbox"]')].find((input) => (input.parentElement?.textContent ?? '').includes('Outside projects')) as HTMLInputElement).click();
		flushSync();
		expect(host.textContent).toContain('CORSIA and agents in Garden plan, Outside projects will be able to use sam.work@example.com.');
		unmount(component);
		component = mount(WhereItWorks, { target: host, props: { app: { ...googleAccount, kind: 'account' as const, reach: { mode: 'selected', project_ids: ['garden'], outside_projects: false } }, onClose: vi.fn(), onSaved: vi.fn() } });
		await settle();
		([...host.querySelectorAll('input[type="checkbox"]')].find((input) => (input.parentElement?.textContent ?? '').includes('Garden plan')) as HTMLInputElement).click();
		([...host.querySelectorAll('input[type="checkbox"]')].find((input) => (input.parentElement?.textContent ?? '').includes('Q4 launch')) as HTMLInputElement).click();
		flushSync();
		expect(host.textContent).toContain("Google — sam.work@example.com will move from Garden plan to Q4 launch, with its sign-in. Agents in Garden plan won't be able to use it.");
	});

	it('saves All projects in the shape the server accepts', async () => {
		const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({ ok: true, json: async () => ({ ...awaiting, state: 'working', revision: 2 }) }));
		vi.stubGlobal('fetch', fetch);
		const onSaved = vi.fn();
		component = mount(WhereItWorks, { target: host, props: { app: { ...awaiting, state: 'working', reach: { mode: 'selected', project_ids: ['q4'], outside_projects: false } }, onClose: vi.fn(), onSaved } });
		await settle();
		(host.querySelector('input[type="radio"]') as HTMLInputElement).click();
		flushSync();
		([...host.querySelectorAll('button')].find((button) => button.textContent?.includes('Save')) as HTMLButtonElement).click();
		await vi.waitFor(() => expect(onSaved).toHaveBeenCalled());
		const call = fetch.mock.calls.find(([input]) => String(input).includes('/reach'));
		expect(JSON.parse(String(call?.[1]?.body)).reach).toEqual({ mode: 'all', project_ids: [], outside_projects: false });
	});

	it('saves an account reach through its account endpoint without an app revision', async () => {
		const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({ ok: true, json: async () => ({ row: googleAccount }) }));
		vi.stubGlobal('fetch', fetch);
		const onSaved = vi.fn();
		component = mount(WhereItWorks, { target: host, props: { app: { ...googleAccount, kind: 'account' as const }, onClose: vi.fn(), onSaved } });
		await settle();
		(host.querySelector('input[type="radio"]') as HTMLInputElement).click();
		flushSync();
		([...host.querySelectorAll('button')].find((button) => button.textContent?.includes('Save')) as HTMLButtonElement).click();
		await vi.waitFor(() => expect(onSaved).toHaveBeenCalled());
		const call = fetch.mock.calls.find(([input]) => String(input).includes('/api/mcp/accounts/google-work/reach'));
		expect(JSON.parse(String(call?.[1]?.body))).toEqual({ reach: { mode: 'all', project_ids: [], outside_projects: false } });
	});

	it("shows the server's Mac notice on the approve sheet only", async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ projects: [] }) })));
		const notice = 'notice text from the server';
		const reach = { mode: 'selected', project_ids: [], outside_projects: false };
		component = mount(WhereItWorks, { target: host, props: { app: { ...awaiting, transport: 'stdio', notice, reach }, approve: true, onClose: vi.fn(), onSaved: vi.fn() } });
		await settle();
		expect(host.textContent).toContain(notice);
		unmount(component);
		component = mount(WhereItWorks, { target: host, props: { app: { ...awaiting, transport: 'http', reach }, approve: true, onClose: vi.fn(), onSaved: vi.fn() } });
		await settle();
		expect(host.textContent).not.toContain("It can do anything you can do on this Mac");
		unmount(component);
		component = mount(WhereItWorks, { target: host, props: { app: { ...awaiting, transport: 'stdio', notice, reach }, onClose: vi.fn(), onSaved: vi.fn() } });
		await settle();
		expect(host.textContent).not.toContain(notice);
	});

	it('names an Only CORSIA account’s current reach under the title, with nothing chosen and Save off', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ projects: [] }) })));
		const line = 'Today, only CORSIA can use this account.';
		component = mount(WhereItWorks, { target: host, props: { app: { ...googleAccount, kind: 'account' as const }, onClose: vi.fn(), onSaved: vi.fn() } });
		await settle();
		const current = [...host.querySelectorAll('p.second')].find((p) => p.textContent === line);
		expect(current).toBeTruthy();
		expect(current?.parentElement?.firstElementChild).toBe(current);
		expect([...host.querySelectorAll<HTMLInputElement>('input[type="checkbox"]')].some((input) => input.checked)).toBe(false);
		expect(([...host.querySelectorAll('button')].find((button) => button.textContent?.includes('Save')) as HTMLButtonElement).disabled).toBe(true);
		unmount(component);
		component = mount(WhereItWorks, { target: host, props: { app: { ...googleAccount, kind: 'account' as const, reach: { mode: 'selected', project_ids: ['garden'], outside_projects: false } }, onClose: vi.fn(), onSaved: vi.fn() } });
		await settle();
		expect(host.textContent).not.toContain(line);
		unmount(component);
		component = mount(WhereItWorks, { target: host, props: { app: { ...awaiting, reach: { mode: 'selected', project_ids: [], outside_projects: false } }, onClose: vi.fn(), onSaved: vi.fn() } });
		await settle();
		expect(host.textContent).not.toContain(line);
		unmount(component);
		// Only CORSIA is its own reach value, never an account with no place chosen.
		component = mount(WhereItWorks, { target: host, props: { app: { ...googleAccount, kind: 'account' as const, reach: { mode: 'selected', project_ids: [], outside_projects: false } }, onClose: vi.fn(), onSaved: vi.fn() } });
		await settle();
		expect(host.textContent).not.toContain(line);
	});

	it('disables Save until an individual target is selected', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ projects: [] }) })));
		component = mount(WhereItWorks, { target: host, props: { app: { ...awaiting, reach: { mode: 'selected', project_ids: [], outside_projects: false } }, onClose: vi.fn(), onSaved: vi.fn() } });
		await settle();
		const save = [...host.querySelectorAll('button')].find((button) => button.textContent?.includes('Save')) as HTMLButtonElement;
		expect(save.disabled).toBe(true);
		expect(host.textContent).toContain('Select at least one project, or remove GitHub.');
	});
});

describe('ConnectedAppsSettings key actions', () => {
	const reach = { mode: 'all', project_ids: [], outside_projects: false };
	const refs = [{ kind: 'env', name: 'API_KEY', entry: 'mcp-stripe-api-key-123', set: false }];

	async function mountRows(servers: Record<string, unknown>[]): Promise<void> {
		resetSettingsNavigation();
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelector('[data-settings-row]')).not.toBeNull());
	}
	const button = (label: string) => [...host.querySelectorAll('button')].find((b) => b.textContent?.trim() === label) as HTMLButtonElement;
	const open = () => { (host.querySelector('.item button.open') as HTMLButtonElement).click(); flushSync(); };

	it('opens the declared key card from the closed and the open row', async () => {
		await mountRows([{ id: 'stripe', name: 'Stripe', state: 'needs_token', revision: 1, reach, key_refs: refs }]);
		button('Set key').click();
		expect(get(settingsNavigation)).toMatchObject({ section: 'keys', anchor: 'key-mcp-stripe-api-key-123', connectedAppKey: { app: 'Stripe', entry: 'mcp-stripe-api-key-123' } });
		resetSettingsNavigation();
		open();
		expect(button('Reconnect')).toBeUndefined();
		button('Set key').click();
		expect(get(settingsNavigation)).toMatchObject({ section: 'keys', connectedAppKey: { app: 'Stripe' } });
	});

	it('falls back to the Keys section from a closed and an open row that declares no key', async () => {
		await mountRows([{ id: 'files', name: 'Files', state: 'needs_token', revision: 1, reach, key_refs: [] }]);
		button('Set key').click();
		expect(get(settingsNavigation)).toMatchObject({ section: 'keys', anchor: null, connectedAppKey: null });
		resetSettingsNavigation();
		open();
		button('Set key').click();
		expect(get(settingsNavigation)).toMatchObject({ section: 'keys', anchor: null, connectedAppKey: null });
	});

	it('offers Replace key and no Reconnect on a rejected key', async () => {
		await mountRows([{ id: 'sentry', name: 'Sentry', state: 'token_rejected', revision: 1, reach }]);
		expect(button('Replace key')).toBeDefined();
		open();
		expect(button('Reconnect')).toBeUndefined();
		button('Replace key').click();
		expect(get(settingsNavigation)).toMatchObject({ section: 'keys', connectedAppKey: null });
	});
});
