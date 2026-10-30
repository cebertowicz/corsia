import { flushSync, mount, unmount } from 'svelte';
import { describe, expect, it, vi } from 'vitest';
import { setupLocalRow } from '$lib/setupProviderRows';
import SetupAiRows, { type AiRow } from './SetupAiRows.svelte';

const action = { label: 'Sign in', run: () => undefined };

describe('setup row progress', () => {
	it('renders the refusal verbatim beside its recovery action and retries', async () => {
		const start = vi.fn();
		const detail = 'Not loaded: needs 12 GB, 8 GB free for a model now';
		const fix = 'CORSIA couldn’t load the model because there isn’t enough free memory. Close other apps, then press Retry.';
		const row = setupLocalRow({ source: { source: 'omlx', name: 'Local model', state: 'needs_setup', local_state: 'start_failed', setup_error_code: 'load_refused', fix, detail }, pick: null, eligible: true, downloading: false, ready: false, startError: '', start, stop: () => {}, check: () => {} });
		const host = document.createElement('div'); document.body.appendChild(host);
		const component = mount(SetupAiRows, { target: host, props: { rows: [row] } }); flushSync();
		expect(host.textContent).toContain(detail);
		expect(host.textContent).toContain(fix);
		host.querySelector<HTMLButtonElement>('button')?.click();
		expect(start).toHaveBeenCalledOnce();
		await unmount(component); host.remove();
	});
	it('renders no more than one spinner for each progress and action shape', async () => {
		const rows: AiRow[] = [
			{ key: 'empty', name: 'Empty', meta: '' },
			{ key: 'busy', name: 'Busy', meta: '', busy: true },
			{ key: 'action', name: 'Action', meta: '', action },
			{ key: 'install', name: 'Installing', meta: '', busy: true, spinning: true, action },
			{ key: 'checking', name: 'Checking', meta: '', busy: true, spinning: true },
			{ key: 'button', name: 'Button', meta: '', action: { ...action, loading: true } },
			{ key: 'both', name: 'Both', meta: '', busy: true, spinning: true, action: { ...action, loading: true } },
			{ key: 'ready', name: 'Ready', meta: '', pill: { state: 'success', label: 'Ready' } },
		];
		const host = document.createElement('div'); document.body.appendChild(host);
		const component = mount(SetupAiRows, { target: host, props: { rows } }); flushSync();
		expect([...host.querySelectorAll('section')].map((row) => row.querySelectorAll('.spinner').length)).toEqual([0, 1, 0, 1, 1, 1, 1, 0]);
		expect(host.querySelector<HTMLButtonElement>('section[aria-label="Both"] button')?.disabled).toBe(true);
		await unmount(component); host.remove();
	});
});
