import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';
import { testedProviders } from '$lib/stores/providers';
import { networkServers } from '$lib/stores/networkServers';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn(async () => undefined) }));
vi.mock('$lib/utils/conductor', () => ({ fetchCorsiaAgent: vi.fn(async () => ({ kind: 'target', target: { thread_id: 'agent' } })) }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
async function settle() { await tick(); await new Promise((done) => setTimeout(done)); await tick(); }
function button(text: string) { const result = [...document.querySelectorAll('button')].find((b) => b.textContent?.trim() === text); if (!result) throw new Error(`Missing button ${text}`); return result; }

async function openSetup(count: number, reachable = true, modelStatus = 'ready', declared = false) {
	let added = false;
	token.set('test-session'); user.set({ id: 'user', name: 'Ada' });
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		if (url === '/api/setup/progress') return json({ setup_completed: false, status: { local_started: true, local_eligible: false, macos_supported: null } });
		if (url.includes('/sources')) return json({ sources: [] });
		if (url.includes('/api/settings/network-servers') && init?.method === 'POST') {
			if (!reachable) return json({ detail: 'Could not reach the endpoint' }, 400);
			if (declared) expect(JSON.parse(String(init.body)).model_ids).toEqual(['declared-model']);
			added = true; return json({ ok: true, server_id: 'server-1', models: count });
		}
		if (url.includes('/api/settings/network-servers')) return json({ servers: added ? [{ id: 'server-1', instance_name: 'My server' }] : [] });
		if (url.startsWith('/api/models')) return json({ models: added && (count || declared) ? [{ id: 'm', status: modelStatus, connection: 'external', provider: 'external', litellm_model: 'external@server-1/model' }] : [] });
		if (url.includes('/api/settings/providers')) return json({ providers: [] });
		return json({});
	}));
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn(), resume: true } });
	flushSync(); await settle(); await settle();
	button('Add provider').click(); await settle();
	([...document.querySelectorAll('button')].find((b) => b.textContent?.includes('Other provider by address')) as HTMLButtonElement).click(); await settle();
	const input = document.querySelector('input[placeholder="https://host.example.com/compatible-mode/v1"]') as HTMLInputElement;
	input.value = 'http://localhost:1234/v1'; input.dispatchEvent(new Event('input', { bubbles: true })); await tick();
	if (declared) {
		button('More options').click(); await tick();
		const names = document.querySelector('textarea[placeholder="model-one, model-two"]') as HTMLTextAreaElement;
		names.value = 'declared-model'; names.dispatchEvent(new Event('input', { bubbles: true })); await tick();
	}
	button('Test & add').click(); await settle(); await settle(); await settle();
}

afterEach(async () => {
	if (component) await unmount(component);
	component = null; host?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals();
	token.set(null); user.set(null); testedProviders.set([]); networkServers.set([]);
});

describe('own endpoint during setup', () => {
	it('enables setup when the endpoint lists a selectable model', async () => {
		await openSetup(1);
		expect(button('Start using CORSIA').disabled).toBe(false);
		expect(host.textContent).toContain('My server · passed a test call');
	});
	it('enables setup for a ready declared model when the model list is empty', async () => {
		await openSetup(0, true, 'ready', true);
		expect(button('Start using CORSIA').disabled).toBe(false);
	});
	it('keeps setup disabled when the listed model has no context window', async () => {
		await openSetup(1, true, 'unavailable');
		expect(button('Start using CORSIA').disabled).toBe(true);
		expect(button('Add provider')).toBeTruthy();
	});
	it('keeps setup disabled and offers adding a provider when no models are listed', async () => {
		await openSetup(0);
		expect(button('Start using CORSIA').disabled).toBe(true);
		expect(button('Add provider')).toBeTruthy();
	});
	it('shows the existing failure and action when the endpoint is unreachable', async () => {
		await openSetup(0, false);
		expect(document.body.textContent).toContain('Could not reach the endpoint');
		expect(button('Test & add').disabled).toBe(false);
		expect(button('Start using CORSIA').disabled).toBe(true);
	});
});
