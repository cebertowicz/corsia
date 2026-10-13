import { afterEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

import ProviderRow from './ProviderRow.svelte';
import type { ProviderEntry } from '$lib/stores/providers';

const externalProvider = (discovery: {
	at: string;
	models: number;
	error: string | null;
	reasoning_levels_error?: string | null;
}) => ({
	name: 'external',
	label: 'OpenAI-compatible API',
	type: 'network' as const,
	statusDot: 'online' as const,
	subline: 'provider.example',
	raw: { name: 'external', label: 'External' } as ProviderEntry,
	server: {
		id: 'ns-external', provider: 'external', instance_id: 'provider.example/v1',
		instance_name: 'External', kind: 'external' as const, dialect: 'openai' as const,
		port: 443, status: 'online' as const, source: 'manual' as const,
		latency_ms: null, memory_total_mb: null, memory_used_mb: null, gpu_info: null,
		endpoints: [], active_host: 'provider.example', api_base: null, mgmt_base: null,
		metadata: { discovery },
	},
});

describe('ProviderRow External discovery warning', () => {
	let component: ReturnType<typeof mount> | null = null;

	afterEach(() => {
		if (component) unmount(component);
		component = null;
		document.body.innerHTML = '';
	});

	it('shows a failed discovery reason below the provider', () => {
		component = mount(ProviderRow, {
			target: document.body,
			props: { provider: externalProvider({ at: '2026-09-24T00:00:00Z', models: 0, error: 'Server is offline' }) },
		});
		flushSync();
		expect(document.querySelector('[role="status"]')?.textContent).toContain('Server is offline');
	});

	it('shows a plain empty-catalog warning but not for discovered models', () => {
		component = mount(ProviderRow, {
			target: document.body,
			props: { provider: externalProvider({ at: '2026-09-24T00:00:00Z', models: 0, error: null }) },
		});
		flushSync();
		expect(document.querySelector('[role="status"]')?.textContent).toBe('Reached, but it listed no models');
		unmount(component);
		component = mount(ProviderRow, {
			target: document.body,
			props: { provider: externalProvider({ at: '2026-09-24T00:00:00Z', models: 1, error: null }) },
		});
		flushSync();
		expect(document.querySelector('[role="status"]')).toBeNull();
	});

	it('says when reasoning levels could not be read', () => {
		component = mount(ProviderRow, {
			target: document.body,
			props: {
				provider: externalProvider({
					at: '2026-09-25T00:00:00Z', models: 2, error: null,
					reasoning_levels_error: 'models.dev could not be read: timed out',
				}),
			},
		});
		flushSync();
		expect(document.querySelector('[role="status"]')?.textContent).toBe(
			'Reasoning levels not updated: models.dev could not be read: timed out',
		);
	});
});

describe('ProviderRow External switch', () => {
	afterEach(() => { document.body.innerHTML = ''; });

	it('reads the endpoint itself, not a provider setting that does not exist', () => {
		const on = externalProvider({ at: '2026-09-24T00:00:00Z', models: 1, error: null });
		const off = { ...on, server: { ...on.server, status: 'disabled' as const } };
		let c = mount(ProviderRow, { target: document.body, props: { provider: on } });
		flushSync();
		expect(document.querySelector('button.tg')?.getAttribute('aria-label')).toBe('Disable');
		unmount(c);
		c = mount(ProviderRow, { target: document.body, props: { provider: off } });
		flushSync();
		expect(document.querySelector('button.tg')?.getAttribute('aria-label')).toBe('Enable');
		unmount(c);
	});
});

describe('ProviderRow harness card', () => {
	afterEach(() => { document.body.innerHTML = ''; });

	it('shows the provider switch with settings and the account/model summary', () => {
		const provider = {
			name: 'codex', label: 'Codex', type: 'harness' as const, statusDot: 'online' as const,
			subline: '2 accounts · 3 models', harnessEnabled: true,
			raw: { name: 'codex', label: 'Codex', type: 'harness' } as ProviderEntry,
		};
		const c = mount(ProviderRow, { target: document.body, props: { provider } });
		flushSync();
		expect(document.querySelector('button.tg')?.getAttribute('aria-label')).toBe('Disable');
		expect(document.querySelector('button[aria-label="Settings"]')).not.toBeNull();
		expect(document.querySelector('.meta')?.textContent).toBe('2 accounts · 3 models');
		expect(document.querySelector('.name')?.textContent).toBe('Codex');
		unmount(c);
	});
});
