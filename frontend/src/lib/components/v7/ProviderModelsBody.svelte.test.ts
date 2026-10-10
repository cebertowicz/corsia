import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';

import ProviderModelsBody from './ProviderModelsBody.svelte';
import { workspaceModels, type Model } from '$lib/stores/models';
import { groupModels } from '$lib/stores/modelGroups';
import { withAccountTurn } from '$lib/stores/modelFixtures';
import type { HostProvider } from '$lib/stores/providersV7';

vi.mock('$lib/stores/harnessProviders', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/harnessProviders')>()),
	harnessModels: async () => [
		{ id: 'claude-code/sonnet', dbId: 'claude-code/sonnet', name: 'Sonnet', state: 'loaded', meta: '', caps: [] },
	],
}));

const provider = {
	name: 'claude-code', label: 'Claude Code', type: 'harness', statusDot: 'online', subline: 'Signed in',
	raw: { name: 'claude-code', label: 'Claude Code', type: 'harness' },
} as HostProvider;

const catalogRow = (over: Partial<Model> & { status_reason?: string }): Model =>
	withAccountTurn({
		id: 'claude-code/sonnet', name: 'Sonnet', size: '', backend: 'network', speed: '',
		status: 'unavailable', status_reason: 'usage_limited', status_resets_at: '2026-10-09T07:23:34+00:00',
		...over,
	});

describe('Settings model rows and account usage', () => {
	let component: ReturnType<typeof mount> | null = null;

	afterEach(() => {
		if (component) unmount(component);
		component = null;
		document.body.innerHTML = '';
		workspaceModels.set([]);
	});

	async function render(): Promise<string> {
		component = mount(ProviderModelsBody, { target: document.body, props: { provider } });
		flushSync();
		await tick();
		await tick();
		return document.body.textContent ?? '';
	}

	it('shows no limit on the model row when the selected account is usage-limited, while the picker group still does', async () => {
		const limited = catalogRow({});
		workspaceModels.set([limited]);

		const text = await render();

		expect(text).toContain('Sonnet');
		expect(text).not.toMatch(/usage limit|resets|checking account|verify account/i);
		expect(document.querySelector('[role="status"]')).toBeNull();
		expect(groupModels([limited])[0].state).toBe('limited');
	});

	it('keeps a model-level warning on the row', async () => {
		workspaceModels.set([catalogRow({ status_reason: 'connection_unavailable' })]);

		const text = await render();

		expect(text).toContain('this harness isn’t connected');
	});
});
