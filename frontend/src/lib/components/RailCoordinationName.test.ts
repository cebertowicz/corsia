import { afterEach, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { allThreads, activeThreadId } from '$lib/stores/threads';
import Header from './Header.svelte';
import RailScopedSection from './RailScopedSection.svelte';

let component: Record<string, unknown> | undefined;
let host: HTMLElement;
afterEach(() => {
	if (component) unmount(component);
	allThreads.set([]);
	activeThreadId.set(null);
	host.remove();
});
it('uses the designated thread stored title for its label', () => {
	host = document.createElement('div');
	document.body.appendChild(host);
	allThreads.set([{ id: 'owner', user_id: 'u', profile: 'personal', title: 'Custom coordinator', model: null, coordination: 'corsia', created_at: 't', updated_at: 't' }]);
	activeThreadId.set('owner');
	component = mount(RailScopedSection, { target: host, props: { conductorThreadId: 'owner' } });
	const headerHost = document.createElement('div');
	host.appendChild(headerHost);
	const header = mount(Header, { target: headerHost, props: { conductorThreadId: 'owner', activeView: 'chat', onToggleSidebar: () => {}, onTogglePane: () => {} } });
	flushSync();
	expect(host.textContent).toContain('Custom coordinator');
	allThreads.update(rows => rows.map(row => ({ ...row, title: 'Renamed coordinator' })));
	flushSync();
	expect(host.textContent).toContain('Renamed coordinator');
	expect(headerHost.textContent).toContain('Renamed coordinator');
	unmount(header);
});
