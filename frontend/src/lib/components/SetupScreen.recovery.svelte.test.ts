import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import SetupScreen from './SetupScreen.svelte';
import { token, user } from '$lib/stores/auth';

vi.mock('$lib/ws', () => ({ connect: vi.fn() }));
vi.mock('$lib/linkTargetResolver', () => ({ openExternalUrl: vi.fn(async () => undefined) }));

const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;

async function settle() { await tick(); await Promise.resolve(); await tick(); }
function click(label: string) {
	const button = Array.from(host.querySelectorAll('button')).find((candidate) => candidate.textContent?.trim() === label);
	expect(button, host.textContent ?? '').toBeTruthy(); button?.click();
}
function fill(id: string, value: string) {
	const input = host.querySelector<HTMLInputElement>(id)!;
	input.value = value; input.dispatchEvent(new Event('input', { bubbles: true }));
}

afterEach(async () => {
	if (component) await unmount(component);
	component = null; host?.remove(); vi.restoreAllMocks(); vi.unstubAllGlobals(); token.set(null); user.set(null);
});

describe('SetupScreen account recovery', () => {
	it('offers a working Save a report action when account creation fails', async () => {
		const invoke = vi.fn(async () => undefined);
		vi.stubGlobal('__TAURI_INTERNALS__', { invoke });
		vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL, init?: RequestInit) => {
			const url = String(input);
			if (url === '/api/setup/status') return json({ apple_silicon: true, macos_supported: true, local_eligible: false, work_folder: '~/CORSIA' });
			if (url === '/api/setup/first-account' && init?.method === 'POST') return json({}, 500);
			return json({});
		}));
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(SetupScreen, { target: host, props: { onExistingAccount: vi.fn(), onAccountCreated: vi.fn(), onFinished: vi.fn() } });
		flushSync(); await settle();
		fill('#setup-name', 'Ada'); fill('#setup-username', 'ada'); fill('#setup-password', 'password1'); fill('#setup-confirm-password', 'password1');
		await settle(); click('Continue'); await settle();
		click('Continue'); await settle();
		expect(host.textContent).toContain('Couldn’t create your account. Retry.');
		click('Save a report'); await settle();
		expect(invoke).toHaveBeenCalledWith('save_setup_report');
	});
});
