import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import * as Y from 'yjs';
import DocViewer from './DocViewer.svelte';
import DocumentBarTestHost from './DocumentBarTestHost.test.svelte';
import { connectionState } from '$lib/stores/connection';
import { emit } from '$lib/ws/events';
import { encodeUpdate } from '$lib/stores/documentWire';
import { startLiveDocuments } from '$lib/stores/liveDocuments';
import { clearDraft } from '$lib/stores/docSaveQueue';
import { recordDocActivity, docPresence } from '$lib/docPresence';
import { displayedDocument } from '$lib/ws/presence';
import { holdCommentArrival, releaseCommentArrivals } from '$lib/stores/docCommentArrivals';
import { heldIds } from '$lib/stores/docCommentHolds';
import type { DocViewerTarget } from '$lib/docViewerTarget';

const api = vi.hoisted(() => ({ fetchContent: vi.fn(), writeContent: vi.fn(), fetchRevision: vi.fn(), fetchComments: vi.fn() }));
vi.mock('$lib/api/docs', async importOriginal => ({ ...await importOriginal<typeof import('$lib/api/docs')>(), ...api }));
const wire = vi.hoisted(() => ({ frames: [] as Record<string, any>[] }));
vi.mock('$lib/ws/state', () => ({ ws: { readyState: 1, send: (text: string) => wire.frames.push(JSON.parse(text)) } }));
vi.mock('$lib/stores/deviceDocument', () => ({ DeviceDocument: class {
	ready = Promise.resolve(); saved = true; error = '';
	destroy = async () => {};
	subscribe(listener: (copy: unknown) => void) { listener(this); return () => {}; }
} }));
vi.mock('$lib/stores/deviceRecovery', () => ({ readDeviceRecovery: async () => [] }));
const presence = vi.hoisted(() => ({ detach: vi.fn(), attached: 0 }));
vi.mock('$lib/docPresenceRuntime', async importOriginal => {
	const original = await importOriginal<typeof import('$lib/docPresenceRuntime')>();
	return { ...original, attachDocPresenceSurface: (...args: Parameters<typeof original.attachDocPresenceSurface>) => {
		presence.attached++;
		const detach = original.attachDocPresenceSurface(...args);
		return () => { presence.detach(); detach(); };
	} };
});

const notes: DocViewerTarget = { instanceId: 'tab-notes', path: '/documents/notes.md', name: 'notes.md', projectId: 'project', threadId: 'thread', artifactId: 'notes' };
let viewer: ReturnType<typeof mount> | undefined;
let bar: ReturnType<typeof mount> | undefined;
let host: HTMLElement;
let barHost: HTMLElement;
let stop: () => void;
let server: Y.Doc;

beforeEach(() => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue(null);
	connectionState.set('connected');
	stop = startLiveDocuments();
	api.fetchContent.mockResolvedValue({ text: 'notes text', writable: true, artifact: { id: 'notes' }, freshness: null });
	api.fetchRevision.mockResolvedValue({ exists: true, writable: true, artifact_id: 'notes', freshness: null });
	api.fetchComments.mockResolvedValue({ comments: [], feedback: { open_count: 0, owner_label: null, undelivered_batches: [] } });
	host = document.createElement('div'); barHost = document.createElement('div');
	document.body.append(host, barHost);
});
afterEach(async () => {
	if (bar) await unmount(bar);
	if (viewer) await unmount(viewer);
	bar = viewer = undefined; host.remove(); barHost.remove(); stop(); server?.destroy();
	clearDraft(notes); docPresence.set({ artifacts: {} }); displayedDocument.set(null); releaseCommentArrivals('notes'); wire.frames.length = 0;
	presence.detach.mockReset(); presence.attached = 0; vi.restoreAllMocks(); vi.unstubAllGlobals();
	for (const mock of Object.values(api)) mock.mockReset();
});

it('keeps document presence alive when the bar moves and an image covers the document', async () => {
	const props = $state<{ target: DocViewerTarget; offScreen: boolean }>({ target: notes, offScreen: false });
	viewer = mount(DocViewer, { target: host, props });
	bar = mount(DocumentBarTestHost, { target: barHost });
	flushSync();
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_open')).toBe(true));
	const request = wire.frames.find(frame => frame.type === 'doc_open')!;
	server = new Y.Doc(); server.getText('text').insert(0, 'notes text');
	emit('doc_opened', { type: 'doc_opened', live: true, writable: true, artifact_id: 'notes', store_id: 'store-notes', request_id: request.request_id, update: encodeUpdate(Y.encodeStateAsUpdate(server)), vector: encodeUpdate(Y.encodeStateVector(server)), pending: false, held: null });
	await vi.waitFor(() => expect(presence.attached).toBe(1));
	displayedDocument.set({ threadId: 'thread', value: { canShow: true, showing: 'notes' } });
	recordDocActivity({ type: 'doc_activity', artifact_id: 'notes', thread_id: 'agent', agent: 'Agent', kind: 'read', span: { from: 1, to: 2 }, comment_id: null, revision: null, at: new Date().toISOString() });
	await vi.waitFor(() => expect(host.querySelector('.cm-presence-cursor')).not.toBeNull());
	await vi.waitFor(() => expect(barHost.querySelector('.presence-row > [aria-label="1 participants"]')).not.toBeNull());
	const measures = barHost.querySelector<HTMLElement>('.presence-row > .measures')!;
	expect(measures.getAttribute('aria-hidden')).toBe('true');
	expect(measures.hasAttribute('inert')).toBe(true);
	expect(measures.querySelectorAll('[id]')).toHaveLength(0);
	const measuringControls = [...measures.querySelectorAll<HTMLElement>('button, a, input, select, textarea, [tabindex]')];
	expect(measuringControls.length).toBeGreaterThan(0);
	expect(measuringControls.every((el) => el.tabIndex === -1)).toBe(true);
	holdCommentArrival('notes', 'held-comment');
	await unmount(bar); bar = undefined;
	bar = mount(DocumentBarTestHost, { target: barHost }); flushSync();
	expect(presence.detach).not.toHaveBeenCalled();
	expect(host.querySelector('.cm-presence-cursor')).not.toBeNull();
	expect(barHost.querySelectorAll('.presence-row > [aria-label="1 participants"]')).toHaveLength(1);
	expect(heldIds('notes')).toContain('held-comment');
	props.offScreen = true; flushSync();
	expect(presence.detach).not.toHaveBeenCalled();
	expect(host.querySelector('.cm-presence-cursor')).not.toBeNull();
	expect(heldIds('notes')).toContain('held-comment');
	props.offScreen = false; flushSync();
	expect(barHost.querySelectorAll('.presence-row > [aria-label="1 participants"]')).toHaveLength(1);
	expect(presence.attached).toBe(1);

	// Leave the bar mounted while the viewer changes to a document without an artifact.
	api.fetchContent.mockReturnValue(new Promise(() => {}));
	props.target = { ...notes, instanceId: 'tab-draft', path: '/documents/draft.md', name: 'draft.md', artifactId: null };
	flushSync();
	expect.soft(barHost.querySelectorAll('.presence-row > [aria-label="1 participants"]').length, 'switch to unregistered document').toBe(0);
	props.target = { ...notes, instanceId: 'tab-other', path: '/documents/other.md', name: 'other.md', artifactId: 'other' };
	flushSync();
	expect.soft(barHost.querySelectorAll('.presence-row > [aria-label="1 participants"]').length, 'switch to another registered document while loading').toBe(0);
	await unmount(viewer); viewer = undefined;
	expect.soft(barHost.querySelectorAll('.presence-row > [aria-label="1 participants"]').length, 'unmount viewer with bar alive').toBe(0);

	api.fetchContent.mockResolvedValue({ text: 'notes text', writable: true, artifact: { id: 'notes' }, freshness: null });
	props.target = notes;
	viewer = mount(DocViewer, { target: host, props }); flushSync();
	await vi.waitFor(() => expect(barHost.querySelectorAll('.presence-row > [aria-label="1 participants"]').length, 'remount has exactly one current stack').toBe(1));
});
