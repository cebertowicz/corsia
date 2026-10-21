import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));

const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
async function settle() { await tick(); await new Promise((done) => setTimeout(done)); await tick(); }

function mountResume(onFinished = vi.fn()) {
	token.set('setup-token'); user.set({ id: 'user', name: 'Ada' });
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const url = String(input);
		if (url === '/api/setup/progress') return json({ setup_completed: false, step: 'models', local_ai: true, status: { local_eligible: true, work_folder: '~/CORSIA' } });
		if (url === '/api/providers/sources' || url === '/api/providers/sources/recheck') return json({ sources: [] });
		if (url.includes('/corsia-agent')) return json({ target: { thread_id: 'corsia-thread' } });
		if (url.includes('/models')) return json({ models: [] });
		return json({});
	}));
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(SetupScreen, { target: host, props: { resume: true, onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished } });
	flushSync();
	return onFinished;
}

afterEach(async () => {
	if (component) await unmount(component);
	host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); token.set(null); user.set(null);
});

describe('SetupScreen resume', () => {
	it('reopens unfinished setup on Providers & models', async () => {
		mountResume(); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Providers & models');
		expect(Array.from(host.querySelectorAll('button')).some((button) => button.textContent?.includes('Start using CORSIA'))).toBe(true);
	});

	it('goes Back from a resumed Providers & models to the subscriptions question, then the work folder', async () => {
		mountResume(); await settle();
		const click = (label: string) => Array.from(host.querySelectorAll('button')).find((button) => button.textContent?.trim() === label)?.click();
		click('Back'); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Your subscriptions');
		expect(host.textContent).toContain('Do you pay for Claude or ChatGPT?');
		click('Back'); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Choose your work folder');
		click('Continue'); await settle();
		expect(host.querySelector('h1')?.textContent).toBe('Your subscriptions');
	});

	it('opens the app directly when the backend records completed setup', async () => {
		const onFinished = vi.fn();
		token.set('setup-token'); user.set({ id: 'user', name: 'Ada' });
		vi.stubGlobal('fetch', vi.fn(async () => json({ setup_completed: true, step: null, local_ai: null })));
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(SetupScreen, { target: host, props: { resume: true, onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished } });
		flushSync(); await settle();
		expect(onFinished).toHaveBeenCalledOnce();
	});
});
