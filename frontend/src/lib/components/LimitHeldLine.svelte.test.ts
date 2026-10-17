import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, describe, expect, it, vi } from 'vitest';

import LimitHeldLine from './LimitHeldLine.svelte';
import type { Model } from '$lib/stores/models';
import { withAccountTurn } from '$lib/stores/modelFixtures';
import { providerCardRequest, settingsNavigation } from '$lib/stores/settingsNavigation';

const VENDORS = [['codex', 'codex/gpt-5-codex'], ['claude-code', 'claude-code/sonnet']] as const;

function row(connection: string, id: string, reason: string, state: 'limited' | 'allowed' = 'limited'): Model {
	return withAccountTurn({
		id, name: id, connection, size: '', backend: 'harness', speed: '', status: state === 'limited' ? 'unavailable' : 'ready',
		usage: { state, usable: state !== 'limited', reason, resets_at: null, windows: [] },
	});
}

describe('LimitHeldLine', () => {
	let component: ReturnType<typeof mount> | null = null;

	afterEach(() => {
		if (component) unmount(component);
		component = null;
		document.body.innerHTML = '';
		providerCardRequest.set(null);
	});

	function render(model: Model | undefined, onSwitchModel = vi.fn()) {
		component = mount(LimitHeldLine, { target: document.body, props: { model, onSwitchModel } });
		flushSync();
		return onSwitchModel;
	}

	const buttons = () => [...document.querySelectorAll('button')].map((b) => b.textContent?.trim());

	it.each(VENDORS)('on %s, the plan reason offers the picker and the account card', (connection, id) => {
		const onSwitchModel = render(row(connection, id, 'plan_limit_overage_off'));
		expect(document.querySelector('.limit-held-text')?.textContent).toBe('Plan limit reached · reset time unknown');
		expect(buttons()).toEqual(['Switch model', 'Spend past plan limit']);

		document.querySelectorAll('button')[0].click();
		expect(onSwitchModel).toHaveBeenCalledOnce();

		const opens = get(settingsNavigation).openRequest;
		document.querySelectorAll('button')[1].click();
		expect(get(providerCardRequest)).toBe(connection);
		expect(get(settingsNavigation).section).toBe('providers');
		expect(get(settingsNavigation).openRequest).toBe(opens + 1);
	});

	it.each(VENDORS)('on %s, a vendor limit offers only the picker', (connection, id) => {
		render(row(connection, id, 'five_hour_limit'));
		expect(document.querySelector('.limit-held-text')?.textContent).toBe('Usage limit reached · reset time unknown');
		expect(buttons()).toEqual(['Switch model']);
	});

	it('stays hidden while the bound model can run', () => {
		render(row('codex', 'codex/gpt-5-codex', 'plan_limit_overage_off', 'allowed'));
		expect(document.querySelector('.limit-held')).toBeNull();
		unmount(component!);
		render(undefined);
		expect(document.querySelector('.limit-held')).toBeNull();
	});
});
