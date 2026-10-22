import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn(async () => undefined) }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
	status, headers: { 'Content-Type': 'application/json' },
});

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let calls: string[];
async function settle() { await tick(); await new Promise((done) => setTimeout(done)); await tick(); }

/** A relaunch after the account step: signed in, setup unfinished, the setup status saying whether a local flight was ever claimed. */
async function resumeWith(localStarted: boolean, startReply: () => Response = () => json({ ok: true, in_progress: true })) {
	token.set('[REDACTED]'); user.set({ id: 'user', name: 'Ada' });
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input); const method = init?.method ?? 'GET'; calls.push(`${method} ${url}`);
		if (url === '/api/setup/progress') {
			return json({ setup_completed: false, status: { apple_silicon: true, macos_supported: true, local_eligible: true, local_started: localStarted, work_folder: '~/CORSIA' } });
		}
		if (url === '/api/providers/local/setup' && method === 'POST') return startReply();
		if (url.includes('/sources')) {
			return json({ sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup', prior_state: null, reason: null, fix: null, checked_at: null, eligible: true, local_state: 'stopped' }] });
		}
		if (url.includes('/models')) return json({ models: [] });
		return json({});
	}));
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn(), resume: true } });
	flushSync(); await settle(); await settle();
}

beforeEach(() => { token.set(null); user.set(null); calls = []; });
afterEach(async () => {
	if (component) await unmount(component);
	host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); token.set(null); user.set(null);
});

describe('resuming setup after the account step', () => {
	it('starts the local download when none was ever started', async () => {
		await resumeWith(false);
		expect(calls.filter((call) => call === 'POST /api/providers/local/setup')).toHaveLength(1);
	});

	it('shows the refusal and Retry when that start is refused', async () => {
		await resumeWith(false, () => json({ ok: false, fix: 'Free up 5 GB, then try again.' }, 500));
		expect(host.textContent).toContain('Free up 5 GB, then try again.');
		expect(host.textContent).not.toContain('Getting the local model ready');
	});

	it('starts nothing when a local download was already started', async () => {
		await resumeWith(true);
		expect(calls.filter((call) => call.startsWith('POST') && call.includes('local'))).toEqual([]);
	});

	it('starts nothing when the status does not say (an older backend)', async () => {
		token.set('[REDACTED]');
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input); calls.push(`${init?.method ?? 'GET'} ${url}`);
			if (url === '/api/setup/progress') return json({ setup_completed: false, status: { local_eligible: true, work_folder: '~/CORSIA' } });
			if (url.includes('/sources')) return json({ sources: [] });
			if (url.includes('/models')) return json({ models: [] });
			return json({});
		}));
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn(), resume: true } });
		flushSync(); await settle(); await settle();
		expect(calls.filter((call) => call.startsWith('POST') && call.includes('local'))).toEqual([]);
	});
});
