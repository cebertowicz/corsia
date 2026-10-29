import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn(async () => undefined) }));

const json = (body: unknown) => new Response(JSON.stringify(body), { headers: { 'Content-Type': 'application/json' } });
let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
let calls: string[];
async function settle() { await tick(); await new Promise((done) => setTimeout(done)); await tick(); }

async function openSetup(reasonCode: 'not_running' | 'no_models', result: Record<string, unknown> = { ok: true, in_progress: true }, refreshed?: Record<string, unknown>) {
	token.set('test-setup-session');
	user.set({ id: 'user', name: 'Ada' });
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
		const url = String(input);
		const method = init?.method ?? 'GET';
		calls.push(`${method} ${url}`);
		if (url === '/api/setup/progress') return json({ setup_completed: false, status: { apple_silicon: true, macos_supported: true, local_eligible: false, local_started: false, work_folder: '~/CORSIA' } });
		if (url === '/api/providers/local/setup' && method === 'POST') return json(result);
		if (url.includes('/sources')) return json({ sources: [{ source: 'omlx', name: 'Local model', state: 'needs_setup', reason_code: reasonCode, eligible: false, local_state: 'stopped', ...(calls.includes('POST /api/providers/local/setup') ? refreshed : {}) }] });
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
	component = null; host?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); token.set(null); user.set(null);
});

describe('Setup local model below the setup floor', () => {
	it('starts a stopped known model when its switch is clicked', async () => {
		await openSetup('not_running');
		expect(calls.filter((call) => call === 'POST /api/providers/local/setup')).toHaveLength(0);
		const toggle = host.querySelector<HTMLButtonElement>('section[aria-label="Local model"] button[aria-label="Local model"]');
		expect(toggle).not.toBeNull();
		toggle!.click();
		await vi.waitFor(() => expect(calls.filter((call) => call === 'POST /api/providers/local/setup')).toHaveLength(1));
	});

	it('hides unknown immediate failure details, shows the reference and offers retry', async () => {
		await openSetup('not_running', { ok: false, message: 'The server could not bind its port.', ref: 'local-123' });
		host.querySelector<HTMLButtonElement>('section[aria-label="Local model"] button[aria-label="Local model"]')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain('CORSIA couldn’t start local setup.'));
		const row = host.querySelector('section[aria-label="Local model"]')!;
		expect(row.textContent).not.toContain('The server could not bind its port.');
		expect(row.textContent).not.toContain('Free the port, then try again.');
		expect(row.textContent).toContain('local-123');
		(row.querySelector('button') as HTMLButtonElement).click();
		await vi.waitFor(() => expect(calls.filter((call) => call === 'POST /api/providers/local/setup')).toHaveLength(2));
	});

	it('shows a refreshed asynchronous failure and offers retry', async () => {
		await openSetup('not_running', { ok: true, in_progress: true }, { local_state: 'failed', setup_error_code: null, setup_error_message: null, setup_error_reference: 'local-fixture', fix: 'CORSIA couldn’t start the local model.' });
		host.querySelector<HTMLButtonElement>('section[aria-label="Local model"] button[aria-label="Local model"]')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain('local-fixture'));
		const row = host.querySelector('section[aria-label="Local model"]')!;
		expect(row.textContent).toContain('CORSIA couldn’t start the local model.');
		expect(row.textContent).toContain('Reference: local-fixture');
		const buttons = [...row.querySelectorAll('button')];
		expect(buttons.some((button) => button.textContent?.trim() === 'Save a report')).toBe(true);
		const retry = buttons.find((button) => button.textContent?.trim() === 'Retry');
		expect(retry).toBeDefined();
		retry!.click();
		await vi.waitFor(() => expect(calls.filter((call) => call === 'POST /api/providers/local/setup')).toHaveLength(2));
	});

	it('shows the no-model refusal after rollback without offering a download', async () => {
		const message = "This computer can't run a local model well. Connect a subscription or an API key in the app.";
		await openSetup('not_running', { ok: true, in_progress: true }, { reason_code: 'no_models', local_state: 'failed', setup_error_code: 'mac_cannot_run', setup_error_message: message });
		host.querySelector<HTMLButtonElement>('section[aria-label="Local model"] button[aria-label="Local model"]')!.click();
		await vi.waitFor(() => expect(host.textContent).toContain('Smaller models and future updates may bring them to computers with less memory.'));
		const row = host.querySelector('section[aria-label="Local model"]')!;
		expect(row.querySelector('button')).toBeNull();
		expect(calls.filter((call) => call.startsWith('POST') && call.includes('local'))).toHaveLength(1);
	});

	it('offers no start or download when there is no known model', async () => {
		await openSetup('no_models');
		const row = host.querySelector('section[aria-label="Local model"]');
		expect(row?.textContent).toContain('Local models need at least 32 GB of memory');
		expect(row?.querySelector('button')).toBeNull();
		expect(calls.filter((call) => call.startsWith('POST') && call.includes('local'))).toEqual([]);
	});
});
