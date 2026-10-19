import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn(async () => undefined) }));

const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
async function settle() { await tick(); await new Promise((done) => setTimeout(done)); await tick(); }

afterEach(async () => {
	if (component) await unmount(component);
	host.remove(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); token.set(null); user.set(null);
});

describe('setup local download', () => {
	it('starts local-model selection and download on the account screen without asking about subscriptions', async () => {
		const calls: string[] = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input); calls.push(`${init?.method ?? 'GET'} ${url}`);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, work_folder: '~/CORSIA' });
			if (url === '/api/setup/local-model' && init?.method === 'POST') return json({ state: 'picked', model: { name: 'Qwen 3.8', download_size_gb: 30 } });
			if (url === '/api/setup/local-download' && init?.method === 'POST') return json({ state: 'downloading' });
			if (url === '/api/setup/local-download') return json({ state: 'downloading', percent: 10, eta_seconds: 720, model: 'Qwen 3.8' });
			return json({});
		}));
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync(); await settle();

		expect(host.querySelector('h1')?.textContent).toBe('Welcome to CORSIA');
		expect(host.textContent).not.toMatch(/subscription question|Do you have/i);
		expect(calls).toContain('POST /api/setup/local-model');
		expect(calls).toContain('POST /api/setup/local-download');
	});

	it('keeps polling the pre-account local download without requiring authenticated source refreshes', async () => {
		vi.useFakeTimers();
		const calls: string[] = [];
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input); calls.push(`${init?.method ?? 'GET'} ${url}`);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: true, work_folder: '~/CORSIA' });
			if (url === '/api/setup/local-model' && init?.method === 'POST') return json({ state: 'picked', model: { name: 'Qwen 3.8', download_size_gb: 30 } });
			if (url === '/api/setup/local-download' && init?.method === 'POST') return json({ state: 'downloading' });
			if (url === '/api/setup/local-download') return json({ state: 'downloading', percent: 10, eta_seconds: 720, model: 'Qwen 3.8' });
			return json({});
		}));
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync(); await vi.advanceTimersByTimeAsync(0); await tick();
		const initialReads = calls.filter((call) => call === 'GET /api/setup/local-download').length;
		await vi.advanceTimersByTimeAsync(3000); await tick();
		expect(calls.filter((call) => call === 'GET /api/setup/local-download')).toHaveLength(initialReads + 1);
	});
});
