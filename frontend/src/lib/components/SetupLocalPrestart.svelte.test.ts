import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn(async () => undefined) }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), {
	status, headers: { 'Content-Type': 'application/json' },
});
const PICKED = { state: 'picked', model: { name: 'Qwen3.8 27B', repository: 'Jundot/Qwen3.8-27B-oQ4e-mtp', download_size_bytes: 17e9, download_size_gb: 17, context_window: 262144 } };

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let calls: string[];
let signedInStarts: number;
async function settle() { await tick(); await new Promise((done) => setTimeout(done)); await tick(); }
function button(label: string) { return Array.from(host.querySelectorAll('button')).find((item) => item.textContent?.trim() === label); }
function setInput(id: string, value: string) {
	const input = host.querySelector<HTMLInputElement>(`#${id}`)!;
	input.value = value;
	input.dispatchEvent(new Event('input', { bubbles: true }));
}

/** A Mac that can run a local model, with the pick and the pre-account start answered by the given handlers. */
function mountSetup(handlers: { pick: () => Promise<Response>; preStart: () => Response; cancelled?: boolean; eligible?: boolean }) {
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input); const method = init?.method ?? 'GET'; calls.push(`${method} ${url}`);
		const eligible = handlers.eligible !== false;
		if (url === '/api/setup/status') return json({ apple_silicon: eligible, macos_supported: true, local_eligible: eligible, work_folder: '~/CORSIA' });
		if (url === '/api/setup/local-model' && method === 'POST') return handlers.pick();
		if (url === '/api/setup/local-download' && method === 'POST') return handlers.preStart();
		if (url === '/api/setup/local-download') return json({ state: 'stopped' });
		if (url === '/api/providers/local/setup') { signedInStarts += 1; return json({ ok: true, in_progress: true }); }
		if (url === '/api/setup/first-account') return json({ token: 'setup-token', user: { id: 'user', name: 'Ada Lovelace' } });
		if (url === '/api/setup/progress') return json({ setup_completed: false, status: { local_eligible: true, work_folder: '~/CORSIA' } });
		if (url.includes('/sources')) {
			const running = signedInStarts > 0;
			return json({ sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup', prior_state: null, reason: null, fix: null, checked_at: null, eligible,
				local_state: running ? 'downloading' : 'stopped', local_cancelled: handlers.cancelled || undefined, download_percent: running ? 0 : undefined }] });
		}
		if (url.includes('/corsia-agent')) return json({ target: { thread_id: 'corsia-thread' } });
		if (url.includes('/models')) return json({ models: [] });
		return json({});
	}));
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
	flushSync();
}

/** Welcome, the account form and the work folder, then past the subscriptions question to Providers & models. */
async function toModels() {
	await settle(); button('Continue')?.click(); await settle();
	setInput('setup-name', 'Ada Lovelace'); setInput('setup-username', 'ada');
	setInput('setup-password', 'password1'); setInput('setup-confirm-password', 'password1'); await settle();
	button('Continue')?.click(); await settle();
	button('Continue')?.click(); await settle();
	if (button('Neither')) { button('Neither')?.click(); await settle(); }
}

beforeEach(() => { token.set(null); user.set(null); calls = []; signedInStarts = 0; });
afterEach(async () => {
	if (component) await unmount(component);
	host.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); token.set(null); user.set(null);
});

describe('the local model starts itself during setup', () => {
	it('shows progress, never a bare Download, while the pick is still being made', async () => {
		let release: (response: Response) => void = () => {};
		mountSetup({ pick: () => new Promise<Response>((done) => { release = done; }), preStart: () => json({ ok: true, in_progress: true }) });
		await toModels();

		expect(button('Download')).toBeUndefined();
		expect(host.textContent).toContain('Getting the local model ready');

		release(json(PICKED)); await settle();
		await vi.waitFor(() => expect(signedInStarts).toBe(1));
		await settle();
		expect(signedInStarts).toBe(1);
		expect(button('Download')).toBeUndefined();
	});

	it('starts the download when Providers & models opens and nothing is running', async () => {
		mountSetup({ pick: async () => json(PICKED), preStart: () => json({ ok: true, in_progress: true }) });
		await toModels();

		await vi.waitFor(() => expect(signedInStarts).toBe(1));
		await settle();
		expect(signedInStarts).toBe(1);
		expect(button('Download')).toBeUndefined();
	});

	it('does not start a download the person stopped', async () => {
		mountSetup({ pick: async () => json(PICKED), preStart: () => json({ ok: true, in_progress: true }), cancelled: true });
		await toModels(); await settle();

		expect(signedInStarts).toBe(0);
		expect(button('Download')).toBeTruthy();
	});

	it('starts nothing and shows no preparing line on a Mac that cannot run the local model', async () => {
		mountSetup({ pick: async () => json(PICKED), preStart: () => json({ ok: true, in_progress: true }), eligible: false });
		await toModels(); await settle();

		expect(host.textContent).not.toContain('Getting the local model ready');
		expect(button('Download')).toBeUndefined();
		expect(signedInStarts).toBe(0);
		expect(calls.filter((call) => call.startsWith('POST') && call.includes('local'))).toEqual([]);
	});

	it('shows a failed pre-start with Retry and does not start it behind the failure', async () => {
		mountSetup({ pick: async () => json(PICKED), preStart: () => json({ ok: false, fix: 'Free up 5 GB, then try again.' }, 500) });
		await toModels();

		expect(host.textContent).toContain('Free up 5 GB, then try again.');
		expect(button('Retry')).toBeTruthy();
		expect(button('Download')).toBeUndefined();
		expect(signedInStarts).toBe(0);
	});
});
