import { afterEach, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { coordinationThreads } from '$lib/stores/threads';
import RailScopedSection from './RailScopedSection.svelte';

let component: Record<string, unknown> | undefined;
let host: HTMLElement;
afterEach(() => {
	if (component) unmount(component);
	coordinationThreads.set([]);
	host.remove();
});
it('uses the designated thread stored title for its label', () => {
	host = document.createElement('div');
	document.body.appendChild(host);
	coordinationThreads.set([{ id: 'owner', user_id: 'u', profile: 'personal', title: 'Custom coordinator', model: null, coordination: 'corsia', created_at: 't', updated_at: 't' }]);
	component = mount(RailScopedSection, { target: host, props: { conductorThreadId: 'owner' } });
	flushSync();
	expect(host.textContent).toContain('Custom coordinator');
});

it('keeps the scope default label when the list has no row for the designated thread', () => {
	host = document.createElement('div');
	document.body.appendChild(host);
	component = mount(RailScopedSection, { target: host, props: { conductorThreadId: 'owner' } });
	flushSync();
	expect(host.textContent).toContain('CORSIA');
	expect(host.textContent).not.toContain('Loading');
});
