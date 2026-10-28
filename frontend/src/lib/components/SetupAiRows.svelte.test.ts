import { flushSync, mount, unmount } from 'svelte';
import { describe, expect, it } from 'vitest';
import SetupAiRows, { type AiRow } from './SetupAiRows.svelte';

const action = { label: 'Sign in', run: () => undefined };

describe('setup row progress', () => {
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
