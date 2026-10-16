import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ConnectedAppsSettings from './ConnectedAppsSettings.svelte';
import WhereItWorks from './WhereItWorks.svelte';
import { token } from '$lib/stores/auth';
import { projects } from '$lib/stores/projects';

const awaiting = { id: 'github', name: 'GitHub', state: 'awaiting_approval', revision: 1, reach: { mode: 'all', project_ids: [], outside_projects: true } };
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
		await vi.waitFor(() => expect(fetch).toHaveBeenCalledWith(expect.stringContaining('/api/mcp/servers/github/approve'), { method: 'POST' }));
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

	it('renders the empty-state copy when there are no apps', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ servers: [] }) })));
		component = mount(ConnectedAppsSettings, { target: host });
		await vi.waitFor(() => expect(host.textContent).toContain('No apps shared with every project yet.'));
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

	it('disables Save until an individual target is selected', async () => {
		vi.stubGlobal('fetch', vi.fn(async () => ({ ok: true, json: async () => ({ projects: [] }) })));
		component = mount(WhereItWorks, { target: host, props: { app: { ...awaiting, reach: { mode: 'selected', project_ids: [], outside_projects: false } }, onClose: vi.fn(), onSaved: vi.fn() } });
		await settle();
		const save = [...host.querySelectorAll('button')].find((button) => button.textContent?.includes('Save')) as HTMLButtonElement;
		expect(save.disabled).toBe(true);
		expect(host.textContent).toContain('Select at least one project, or remove GitHub.');
	});
});
