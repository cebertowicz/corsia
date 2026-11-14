import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import DocOwnerControl from './DocOwnerControl.svelte';
import { changeOwnership } from '$lib/api/docOwnership';
import type { DocOwnership } from '$lib/api/docOwnership';

vi.mock('$lib/api/docOwnership', async (original) => ({ ...(await original<typeof import('$lib/api/docOwnership')>()), changeOwnership: vi.fn() }));

// A row that turns inert is waiting on a load, whether the tab switched target or the same document reloaded; nothing it offered may still act.
const choice = (id: string, name: string) => ({ id, name, status: 'ready', responsibility: '' });
const value: DocOwnership = {
	owner: { kind: 'agent', id: 'a1', name: 'Release planning', label: 'Release planning' },
	supervisor: { id: 'a2', name: 'Research coordinator', source: 'picked', status: 'ready' },
	can_act: true, multi_person: false, can_change: true, refusal: null, multi_person_refusal: null, scope: 'project',
	choices: [choice('a1', 'Release planning'), choice('a2', 'Research coordinator')],
};
const changed = vi.mocked(changeOwnership);
let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;
async function settle() { flushSync(); for (let i = 0; i < 4; i++) await tick(); flushSync(); }
afterEach(() => { if (component) unmount(component); component = undefined; host?.remove(); document.body.querySelectorAll('[data-popover]').forEach((node) => node.remove()); changed.mockReset(); });
const picker = () => document.body.querySelector<HTMLElement>('.role-picker');
const items = () => [...host.querySelectorAll<HTMLButtonElement>('button.item')];

describe('DocOwnerControl when its row turns inert', () => {
	it('closes an open picker and writes nothing', async () => {
		const props = $state({ artifactId: 'doc-a', ownership: value, inert: false, onChange: () => {} });
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(DocOwnerControl, { target: host, props });
		await settle();
		host.querySelector<HTMLButtonElement>('button.item[data-role="owner"]')!.click(); await settle();
		expect(picker()).not.toBeNull();
		props.artifactId = 'doc-b'; props.inert = true; await settle();
		expect(picker()).toBeNull();
		expect(items().every((button) => button.disabled)).toBe(true);
		expect(changed).not.toHaveBeenCalled();
	});
});
