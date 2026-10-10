import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import DocDetailPane from './DocDetailPane.svelte';
import { docLibrary, docSources } from '$lib/stores/docLibrary';
import { selectedDocId } from '$lib/stores/documentsSurface';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	docSources.set([{ id: 'workspace', label: 'Workspace', kind: 'workspace', path: '/workspace' }]);
	docLibrary.set([{
		id: '/workspace/notes.md',
		artifactId: 'artifact-1',
		sourceId: 'workspace',
		path: 'notes.md',
		abs: '/workspace/notes.md',
		name: 'notes.md',
		description: 'Notes',
		descState: 'edited',
		tokens: 10,
		updatedAt: '2026-07-13T00:00:00Z',
		updatedTs: Date.parse('2026-07-13T00:00:00Z'),
		updatedLabel: 'today',
		searchKey: 'notes.md notes',
		ownerThread: 'Owning thread',
		ownerThreadId: 'owner-1',
	}]);
	selectedDocId.set('/workspace/notes.md');
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	docLibrary.set([]);
	docSources.set([]);
	selectedDocId.set(null);
});

describe('DocDetailPane ownership copy', () => {
	it('labels the registry owner without implying authorship', () => {
		component = mount(DocDetailPane, { target: host });
		flushSync();

		expect(host.textContent).toContain('Owned by');
		expect(host.textContent).not.toContain('Written by');
		expect(host.textContent).toContain('Owning thread');
	});
});
