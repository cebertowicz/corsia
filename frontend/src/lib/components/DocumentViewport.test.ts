import { afterEach, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import { get } from 'svelte/store';
import DocumentViewport from './DocumentViewport.svelte';
import { openArtifactTab, threadWorkspaces } from '$lib/stores/artifactWorkspace';
import { viewingDocumentForThread } from '$lib/stores/documentViewport';
import { viewingHeading } from '$lib/cm/viewing-heading';

vi.mock('$lib/cm/viewing-heading', () => ({ viewingHeading: vi.fn(() => ({ anchor: 7, line: '## Seven' })) }));
let component: ReturnType<typeof createClassComponent> | null = null;
afterEach(() => { component?.$destroy(); component = null; threadWorkspaces.set({}); });

it('reads a mounted document at send time and retires its reader on loading and unmount', () => {
	openArtifactTab('main', { artifactId: 'design', path: 'design.md', title: 'Design' });
	const instanceId = get(threadWorkspaces).main.activeTabId!;
	const root = document.createElement('div');
	component = createClassComponent({ component: DocumentViewport, target: root, props: { instanceId, root, ready: true } });
	flushSync();
	expect(viewingDocumentForThread('main')).toEqual({ artifact_id: 'design', heading_anchor: 7, heading_line: '## Seven' });
	expect(viewingHeading).toHaveBeenCalledWith(root);
	vi.mocked(viewingHeading).mockReturnValue({ anchor: 11, line: '## Eleven' });
	expect(viewingDocumentForThread('main')?.heading_anchor).toBe(11);
	component.$set({ ready: false });
	flushSync();
	expect(viewingDocumentForThread('main')).toEqual({ artifact_id: 'design' });
	component.$set({ ready: true });
	flushSync();
	expect(viewingDocumentForThread('main')?.heading_anchor).toBe(11);
	component.$destroy(); component = null;
	expect(viewingDocumentForThread('main')).toEqual({ artifact_id: 'design' });
});
