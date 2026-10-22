import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import ArtifactTabStrip from './ArtifactTabStrip.svelte';
import type { ArtifactTab } from '$lib/stores/artifactWorkspace';

const tab = (id: string): ArtifactTab => ({
	kind: 'document', instanceId: id, artifactId: `art-${id}`, path: `/${id}.md`, title: `${id}.md`, threadId: null,
	loadedRevision: 'r1', latestRevision: 'r1', status: 'ready',
});

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
let original: PropertyDescriptor | undefined;
const scrollIntoView = vi.fn();

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	original = Object.getOwnPropertyDescriptor(HTMLElement.prototype, 'scrollIntoView');
	Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', { configurable: true, value: scrollIntoView });
	scrollIntoView.mockClear();
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	if (original) Object.defineProperty(HTMLElement.prototype, 'scrollIntoView', original);
	else Reflect.deleteProperty(HTMLElement.prototype, 'scrollIntoView');
});

describe('ArtifactTabStrip reveal', () => {
	it('reveals the active tab when a tab opens, not when a save replaces the same tabs', async () => {
		const props = $state({ tabs: [tab('a'), tab('b')], activeKey: 'b' as string | null });
		component = mount(ArtifactTabStrip, {
			target: host,
			props: {
				get tabs() { return props.tabs; },
				get activeKey() { return props.activeKey; },
				onSelect: () => {}, onClose: () => {}, onCollapse: () => {},
			},
		});
		flushSync();
		await tick();
		expect(scrollIntoView).toHaveBeenCalledOnce();

		scrollIntoView.mockClear();
		props.tabs = props.tabs.map((t) => (t.instanceId === 'b' ? { ...t, loadedRevision: 'r2', latestRevision: 'r2' } : t));
		flushSync();
		await tick();
		expect(scrollIntoView).not.toHaveBeenCalled();

		props.tabs = [...props.tabs, tab('c')];
		props.activeKey = 'c';
		flushSync();
		await tick();
		expect(scrollIntoView).toHaveBeenCalledOnce();
	});
});
