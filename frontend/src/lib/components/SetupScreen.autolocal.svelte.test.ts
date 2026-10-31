import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
vi.mock('$lib/ws/events', () => ({ on: vi.fn(() => () => undefined) }));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn(async () => undefined) }));

const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let calls: string[];
let codex: Record<string, unknown>;
let localRow: Record<string, unknown> | null;
let installFails: boolean;

async function settle() { await vi.advanceTimersByTimeAsync(10); await tick(); }
function click(label: string) {
	const button = Array.from(host.querySelectorAll('button')).find((candidate) => candidate.textContent?.trim() === label);
	expect(button, host.textContent ?? '').toBeTruthy(); button?.click();
}
const localPosts = () => calls.filter((call) => call.startsWith('POST') && ['/api/providers/local/setup', '/api/setup/local-download'].some((url) => call.endsWith(url))).length;

function mountResume() {
	token.set('setup-token'); user.set({ id: 'user', name: 'Ada' });
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input), method = init?.method ?? 'GET'; calls.push(`${method} ${url}`);
		if (url === '/api/setup/progress') return json({ setup_completed: false, step: 'models', local_ai: false, status: { local_eligible: true, work_folder: '~/CORSIA' } });
		if (url === '/api/providers/sources' || url === '/api/providers/sources/recheck') return json({ sources: [codex, ...(localRow ? [localRow] : [])] });
		if (url.startsWith('/api/corsia-agent')) return json({ target: { thread_id: 'corsia-thread' } });
		if (url.startsWith('/api/models?')) return json({ models: [] });
		if (url === '/api/settings/network-servers') return new Response('{}', { status: 500 });
		if (url.startsWith('/api/vendor-accounts?')) return json({ accounts: [] });
		if (url === '/api/vendor-accounts' && method === 'POST') return json({ id: 'account-1' });
		if (url === '/api/vendor-accounts/account-1/signin' && method === 'POST') return json({ status: 'failed', cause: 'vendor_login_failed' });
		if (url === '/api/providers/cli/codex/install' && method === 'POST') return json({ state: 'running', reason: null, ref: null, path_shadowed_by: null });
		if (url === '/api/providers/cli/codex/install') return json({ state: installFails ? 'failed' : 'running', reason: installFails ? 'the installer stopped' : null, ref: null, path_shadowed_by: null });
		return json({ ok: true });
	}));
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(SetupScreen, { target: host, props: { resume: true, onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
	flushSync();
}

beforeEach(() => {
	vi.useFakeTimers(); calls = []; installFails = false; localRow = null;
	codex = { source: 'codex', name: 'Codex', state: 'needs_auth' };
});
afterEach(async () => {
	if (component) await unmount(component);
	component = null; host?.remove(); vi.useRealTimers(); vi.restoreAllMocks(); vi.unstubAllGlobals(); token.set(null); user.set(null);
});

describe('SetupScreen free local download after a failed CLI sign-in', () => {
	it('starts once, however many sign-ins fail', async () => {
		mountResume(); await settle();
		expect(localPosts()).toBe(0);
		click('Sign in'); await settle();
		expect(localPosts()).toBe(1);
		click('Sign in'); await settle();
		expect(host.textContent).toContain('Sign-in didn’t finish in your browser');
		expect(localPosts()).toBe(1);
	});

	it('does not start for an install failure', async () => {
		codex = { source: 'codex', name: 'Codex', state: 'not_installed' }; installFails = true;
		mountResume(); await settle(); click('Sign in'); await settle();
		await vi.advanceTimersByTimeAsync(3000); await tick();
		expect(host.textContent).toContain('Couldn’t install Codex');
		expect(localPosts()).toBe(0);
	});

	it('does not start over a failed local row', async () => {
		localRow = { source: 'omlx', name: 'Local model', state: 'needs_setup', local_state: 'start_failed', setup_error_code: 'start_failed', fix: 'CORSIA couldn’t set up the local model.' };
		mountResume(); await settle(); click('Sign in'); await settle();
		expect(host.textContent).toContain('Sign-in didn’t finish in your browser');
		expect(localPosts()).toBe(0);
	});

	it('does not start when the person chose never local', async () => {
		localRow = { source: 'omlx', name: 'Local model', state: 'needs_setup', never_local: true };
		mountResume(); await settle(); click('Sign in'); await settle();
		expect(host.textContent).toContain('Sign-in didn’t finish in your browser');
		expect(localPosts()).toBe(0);
	});
});
