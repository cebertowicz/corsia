import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import DocViewer from './DocViewer.svelte';
import { connectionState } from '$lib/stores/connection';
import { emit } from '$lib/ws/events';
import { startLiveDocuments } from '$lib/stores/liveDocuments';
import { clearDraft } from '$lib/stores/docSaveQueue';
import { fetchHistory, fetchVersionText } from '$lib/docHistory';

const api = vi.hoisted(() => ({ fetchContent: vi.fn(), writeContent: vi.fn(), fetchRevision: vi.fn(), fetchComments: vi.fn() }));
vi.mock('$lib/api/docs', async importOriginal => ({ ...await importOriginal<typeof import('$lib/api/docs')>(), ...api }));
vi.mock('$lib/docHistory', async importOriginal => ({ ...await importOriginal<typeof import('$lib/docHistory')>(), fetchHistory: vi.fn(), fetchVersionText: vi.fn() }));
vi.mock('$lib/product_events', () => ({ emitProductEvent: vi.fn() }));
const wire = vi.hoisted(() => ({ frames: [] as Record<string, any>[] }));
vi.mock('$lib/ws/state', () => ({ ws: { readyState: 1, send: (text: string) => wire.frames.push(JSON.parse(text)) } }));
vi.mock('$lib/stores/deviceDocument', () => ({ DeviceDocument: class {
	ready = Promise.resolve(); saved = true; error = '';
	destroy = async () => {};
	subscribe(listener: (copy: unknown) => void) { listener(this); return () => {}; }
} }));
vi.mock('$lib/stores/deviceRecovery', () => ({ readDeviceRecovery: async () => [] }));

const target = { instanceId: 'history-viewer', path: '/documents/doc.md', name: 'doc.md', projectId: 'project', threadId: 'thread', artifactId: 'artifact' };
let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;
let stop: () => void;
const flush = async () => { for (let i = 0; i < 6; i++) { await Promise.resolve(); await tick(); flushSync(); } };
const restore = () => Array.from(document.querySelectorAll<HTMLButtonElement>('button')).find(button => button.textContent?.trim() === 'Restore');

beforeEach(() => {
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue(null);
	Element.prototype.scrollTo = vi.fn();
	connectionState.set('connected');
	stop = startLiveDocuments();
	api.fetchRevision.mockResolvedValue({ exists: true, writable: false, artifact_id: 'artifact', freshness: null });
	api.fetchComments.mockResolvedValue({ comments: [], feedback: { open_count: 0, owner_label: null, undelivered_batches: [] } });
	vi.mocked(fetchHistory).mockResolvedValue({ versions: [0, 1, 2].map(index => ({ id: `v${index}`, author: `Author ${index}`, savedAt: '2026-11-03T16:02:00Z', reason: null })), truncated: false });
	vi.mocked(fetchVersionText).mockImplementation(async (_target, id) => `Version ${id}\n`);
});
afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	stop();
	clearDraft(target);
	wire.frames.length = 0;
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	for (const mock of Object.values(api)) mock.mockReset();
});

describe('version history in the viewer', () => {
	async function openOldVersion(writable: boolean) {
		api.fetchContent.mockResolvedValue({ text: 'disk copy', writable, history: true, artifact: { id: 'artifact' }, freshness: null, live: false });
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(DocViewer, { target: host, props: { target } });
		flushSync();
		await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_open')).toBe(true));
		emit('doc_opened', { type: 'doc_opened', request_id: wire.frames.findLast(frame => frame.type === 'doc_open')!.request_id, live: false });
		await vi.waitFor(() => expect(host.querySelector('.history-tool')).not.toBeNull());
		host.querySelector<HTMLButtonElement>('.history-tool')!.click(); await flush();
		await vi.waitFor(() => expect(document.querySelectorAll('.vrow').length).toBe(3));
		document.querySelectorAll<HTMLElement>('.vrow')[2].click(); await flush();
	}

	it('tells History a file the server says is read-only, so Restore says why instead of sending', async () => {
		await openOldVersion(false);
		expect(restore()?.getAttribute('aria-label')).toBe('Restore: this file is read-only');
	});

	it('leaves Restore available on a file the server says is writable', async () => {
		await openOldVersion(true);
		expect(restore()?.getAttribute('aria-disabled')).not.toBe('true');
	});
});
