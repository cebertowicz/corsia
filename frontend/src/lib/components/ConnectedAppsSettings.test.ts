import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ConnectedAppsSettings from './ConnectedAppsSettings.svelte';
import WhereItWorks from './WhereItWorks.svelte';
import { token } from '$lib/stores/auth';
import { projects } from '$lib/stores/projects';

const awaiting = { id: 'github', name: 'GitHub', state: 'awaiting_approval', revision: 1, reach: { mode: 'all', project_ids: [], outside_projects: false } };
let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
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
	token.set(null);
});

async function settle(): Promise<void> { await Promise.resolve(); await Promise.resolve(); flushSync(); }

describe('ConnectedAppsSettings', () => {
	it('renders the approval row and calls approve', async () => {
		const fetch = vi.fn(async (input: RequestInfo | URL) => ({ ok: true, json: async () => String(input).includes('/approve') ? { ...awaiting, state: 'working', tool_count: 2 } : { servers: [awaiting] } }));
		vi.stubGlobal('fetch', fetch);
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('Waiting for your approval'));
		(host.querySelector('button.btn.primary') as HTMLButtonElement).click();
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/mcp/servers/github/approve'), expect.objectContaining({ method: 'POST', body: JSON.stringify({ expected_revision: 1 }) })));
		await vi.waitFor(() => expect(host.textContent).toContain('Connected · 2 actions'));
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
		await vi.waitFor(() => expect(host.querySelectorAll('.waiting-row').length).toBe(2));
		([...host.querySelectorAll('.waiting-row')[0].querySelectorAll('button')].find((button) => button.textContent?.includes('Decline')) as HTMLButtonElement).click();
		await vi.waitFor(() => expect(host.querySelectorAll('.waiting-row').length).toBe(1));
		expect(host.querySelector('.waiting-copy strong')?.textContent).toBe('Linear');
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
		expect(host.querySelector('.waiting-copy .status')?.textContent).toBe('Waiting for your approval');
		const labels = [...host.querySelectorAll('.waiting-actions button')].map((button) => button.textContent?.trim());
		expect(labels).toEqual(['Choose where it works…', 'Decline']);
	});

	it('shows the folder label as plain text under the status', async () => {
		const folderApp = { ...awaiting, id: 'postgres', name: 'Postgres', folder: '<b>shop</b>', reach: { mode: 'selected', project_ids: [], outside_projects: false } };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [folderApp] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('From Claude Code · <b>shop</b> folder'));
		expect(host.querySelector('.waiting-copy b')).toBeNull();
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
		await vi.waitFor(() => expect(host.querySelectorAll('.waiting-row').length).toBe(3));
		for (const row of host.querySelectorAll('.waiting-row')) {
			expect(row.querySelector('.waiting-panel')).toBeNull();
			expect([...row.children].map((child) => child.classList.contains('waiting-copy') ? 'copy' : child.classList.contains('waiting-actions') ? 'actions' : 'other')).toEqual(['copy', 'actions']);
		}
	});

	it('still shows the sign-in panel on a waiting row once there is something to say', async () => {
		const pending = { ...awaiting, id: 'notes', name: 'Notes', transport: 'http', sign_in: { status: 'pending', signed_in: false }, reach: { mode: 'selected', project_ids: [], outside_projects: false } };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [pending] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelector('.waiting-panel')).not.toBeNull());
	});

	it('lists the rows in the order the server sends them', async () => {
		const place = { mode: 'selected', project_ids: [], outside_projects: false };
		const rows = ['Acme', 'Linear', 'Notes', 'Postgres'].map((name) => ({ ...awaiting, id: name.toLowerCase(), name, reach: place }));
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: rows }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelectorAll('.waiting-row').length).toBe(4));
		expect([...host.querySelectorAll('.waiting-copy strong')].map((el) => el.textContent)).toEqual(['Acme', 'Linear', 'Notes', 'Postgres']);
	});

	it('offers only Dismiss on an expired request', async () => {
		const expired = { ...awaiting, state: 'expired', reach: { mode: 'selected', project_ids: [], outside_projects: false } };
		const fetch = vi.fn(async (_input: RequestInfo | URL, _init?: RequestInit) => ({ ok: true, json: async () => ({ servers: [expired] }) }));
		vi.stubGlobal('fetch', fetch);
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('Request expired'));
		expect(host.textContent).not.toContain('Choose where it works…');
		expect(host.textContent).not.toContain('Reconnect');
		(host.querySelector('.app-row button') as HTMLButtonElement).click();
		flushSync();
		expect([...host.querySelectorAll('.app-row .actions button')].map((button) => button.textContent?.trim())).toEqual(['Dismiss']);
		(host.querySelector('.app-row .actions button') as HTMLButtonElement).click();
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/mcp/servers/github/decline'), { method: 'POST' }));
	});

	it('shows a pending change on a working app as Approve and Decline, with no place action', async () => {
		const pending = { ...awaiting, tool_count: 3, reach: { mode: 'selected', project_ids: ['q4'], outside_projects: false } };
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [pending] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('Waiting for your approval'));
		const labels = [...host.querySelectorAll('.waiting-actions button')].map((button) => button.textContent?.trim());
		expect(labels).toEqual(['Approve', 'Decline']);
		expect(host.textContent).not.toContain('Choose where it works…');
	});

	it('shows a failed action on its own row only', async () => {
		const second = { ...awaiting, id: 'linear', name: 'Linear' };
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => String(input).includes('/decline')
			? { ok: false, status: 500, json: async () => ({ detail: 'nope' }), text: async () => 'nope' }
			: { ok: true, json: async () => ({ servers: [awaiting, second] }) }));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.querySelectorAll('.waiting-row').length).toBe(2));
		([...host.querySelectorAll('.waiting-row')[1].querySelectorAll('button')].find((b) => b.textContent?.includes('Decline')) as HTMLButtonElement).click();
		await vi.waitFor(() => expect(host.querySelectorAll('.waiting-row .error').length).toBe(1));
		expect(host.querySelectorAll('.waiting-row')[1].querySelector('.error')).not.toBeNull();
		expect(host.querySelectorAll('.waiting-row')[0].querySelector('.error')).toBeNull();
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

	it('renders the empty-state copy when there are no apps', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('No connected apps yet. Ask CORSIA in chat'));
		expect(host.textContent).not.toContain('Connect one here');
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

	it('shows the Mac notice on the approve sheet for a stdio app only', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ projects: [] }) })));
		const notice = "It can do anything you can do on this Mac, except open CORSIA's own files and saved keys.";
		const reach = { mode: 'selected', project_ids: [], outside_projects: false };
		component = mount(WhereItWorks, { target: host, props: { app: { ...awaiting, transport: 'stdio', reach }, approve: true, onClose: vi.fn(), onSaved: vi.fn() } });
		await settle();
		expect(host.textContent).toContain(notice);
		unmount(component);
		component = mount(WhereItWorks, { target: host, props: { app: { ...awaiting, transport: 'http', reach }, approve: true, onClose: vi.fn(), onSaved: vi.fn() } });
		await settle();
		expect(host.textContent).not.toContain(notice);
		unmount(component);
		component = mount(WhereItWorks, { target: host, props: { app: { ...awaiting, transport: 'stdio', reach }, onClose: vi.fn(), onSaved: vi.fn() } });
		await settle();
		expect(host.textContent).not.toContain(notice);
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
