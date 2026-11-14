import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import { EditorView } from '@codemirror/view';
import * as Y from 'yjs';
import DocViewer from './DocViewer.svelte';
import { connectionState } from '$lib/stores/connection';
import { emit } from '$lib/ws/events';
import { encodeUpdate } from '$lib/stores/documentWire';
import { startLiveDocuments } from '$lib/stores/liveDocuments';
import { clearDraft } from '$lib/stores/docSaveQueue';
import { clearTabViewState } from '$lib/stores/tabViewState';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import { changeOwnership, type DocOwnership } from '$lib/api/docOwnership';

// A tab return draws the owner and supervisor row the tab last showed in its first frame; the load that follows replaces it.
const api = vi.hoisted(() => ({ fetchContent: vi.fn(), writeContent: vi.fn(), fetchRevision: vi.fn(), fetchComments: vi.fn() }));
vi.mock('$lib/api/docs', async importOriginal => ({ ...await importOriginal<typeof import('$lib/api/docs')>(), ...api }));
const wire = vi.hoisted(() => ({ frames: [] as Record<string, any>[] }));
vi.mock('$lib/ws/state', () => ({ ws: { readyState: 1, send: (text: string) => wire.frames.push(JSON.parse(text)) } }));
vi.mock('$lib/stores/deviceDocument', () => ({ DeviceDocument: class {
	ready = Promise.resolve(); saved = true; error = '';
	destroy = async () => {};
	subscribe(listener: (copy: unknown) => void) { listener(this); return () => {}; }
} }));
vi.mock('$lib/api/docOwnership', async importOriginal => ({ ...await importOriginal<typeof import('$lib/api/docOwnership')>(), changeOwnership: vi.fn() }));
vi.mock('$lib/stores/deviceRecovery', () => ({ readDeviceRecovery: async () => [] }));

const notes: DocViewerTarget = { instanceId: 'tab-notes', path: '/documents/notes.md', name: 'notes.md', projectId: 'project', threadId: 'thread', artifactId: 'notes' };
const other: DocViewerTarget = { instanceId: 'tab-other', path: '/documents/other.md', name: 'other.md', projectId: 'project', threadId: 'thread', artifactId: 'other' };
const texts: Record<string, string> = { notes: 'notes text', other: 'other text' };
const ownedBy = (name: string): DocOwnership => ({
	owner: { kind: 'agent', id: 'a1', name, label: name },
	supervisor: { id: 'a2', name: 'Research coordinator', source: 'picked', status: 'ready' },
	can_act: true, multi_person: false, can_change: true, refusal: null, multi_person_refusal: null, scope: 'project',
	choices: [{ id: 'a1', name, status: 'ready', responsibility: '' }, { id: 'a2', name: 'Research coordinator', status: 'ready', responsibility: '' }],
});
let component: ReturnType<typeof mount> | undefined;
let host: HTMLElement;
let stop: () => void;
const docs: Y.Doc[] = [];
/** What the next load of notes answers; null holds it until `answerNotes` runs. */
let answerNotes: ((ownership: DocOwnership | null) => void) | null = null;
/** Fails the held load of notes the way a broken load does. */
let failNotes: (() => void) | null = null;
const owner = () => host.querySelector<HTMLButtonElement>('[data-ownership-row] button.item[data-role="owner"]');
const ownerLabel = () => owner()?.getAttribute('aria-label') ?? null;

beforeEach(() => {
	answerNotes = null; failNotes = null;
	vi.stubGlobal('ResizeObserver', class { observe() {} unobserve() {} disconnect() {} });
	vi.spyOn(EditorView.prototype, 'coordsAtPos').mockReturnValue(null);
	connectionState.set('connected');
	stop = startLiveDocuments();
	api.fetchRevision.mockResolvedValue({ exists: true, writable: true, artifact_id: 'notes', freshness: null });
	api.fetchComments.mockResolvedValue({ comments: [], feedback: { open_count: 0, owner_label: null, undelivered_batches: [] } });
});
afterEach(async () => {
	if (component) await unmount(component);
	component = undefined;
	host?.remove();
	stop();
	clearDraft(notes); clearDraft(other);
	clearTabViewState('thread', notes.instanceId); clearTabViewState('thread', other.instanceId);
	wire.frames.length = 0;
	for (const doc of docs.splice(0)) doc.destroy();
	vi.restoreAllMocks();
	vi.unstubAllGlobals();
	for (const mock of Object.values(api)) mock.mockReset();
});

/** Answers the newest open of `artifact` with its live document. */
async function serverOpen(artifact: string) {
	await vi.waitFor(() => expect(wire.frames.some(frame => frame.type === 'doc_open' && frame.path.includes(artifact))).toBe(true));
	const request = wire.frames.findLast(frame => frame.type === 'doc_open' && frame.path.includes(artifact))!;
	wire.frames.splice(wire.frames.indexOf(request), 1);
	const server = new Y.Doc(); docs.push(server); server.getText('text').insert(0, texts[artifact]);
	emit('doc_opened', { type: 'doc_opened', live: true, writable: true, artifact_id: artifact, store_id: `store-${artifact}`,
		request_id: request.request_id, update: encodeUpdate(Y.encodeStateAsUpdate(server)), vector: encodeUpdate(Y.encodeStateVector(server)), pending: false, held: null });
	await vi.waitFor(() => expect(host.querySelector('.cm-content')?.textContent).toBe(texts[artifact]));
}

/** Opens notes (owned by Release planning), moves to other, and returns to notes with the load held. */
async function returnToNotes() {
	api.fetchContent.mockImplementation(async (path: string) => {
		if (path.includes('other')) return { text: texts.other, writable: true, artifact: { id: 'other' }, freshness: null };
		if (!answerNotes) return { text: texts.notes, writable: true, artifact: { id: 'notes' }, freshness: null, ownership: ownedBy('Release planning') };
		return new Promise((resolve, reject) => { failNotes = () => reject(new Error('500 server error')); const held = answerNotes; answerNotes = (ownership) => { held?.(ownership); resolve({ text: texts.notes, writable: true, artifact: { id: 'notes' }, freshness: null, ownership: ownership ?? undefined }); }; });
	});
	const props = $state({ target: notes });
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(DocViewer, { target: host, props });
	flushSync();
	await serverOpen('notes');
	await vi.waitFor(() => expect(ownerLabel()).toBe('Owner: Release planning'));
	props.target = other;
	flushSync();
	await serverOpen('other');
	// Other has no owner row; the next load of notes is held until the test answers it.
	await vi.waitFor(() => expect(ownerLabel()).toBeNull());
	answerNotes = () => {};
	props.target = notes;
	flushSync();
}

describe('the owner and supervisor row on a tab return', () => {
	it('is drawn in the first frame, with its pickers waiting, and the loaded answer replaces it', async () => {
		await returnToNotes();
		// Before the load has answered: the row the tab showed, but nothing in it acts yet.
		expect(ownerLabel()).toBe('Owner: Release planning');
		for (const button of host.querySelectorAll<HTMLButtonElement>('[data-ownership-row] button.item')) expect(button.disabled).toBe(true);
		answerNotes!(ownedBy('Release planning, renamed'));
		await vi.waitFor(() => expect(ownerLabel()).toBe('Owner: Release planning, renamed'));
		for (const button of host.querySelectorAll<HTMLButtonElement>('[data-ownership-row] button.item')) expect(button.disabled).toBe(false);
	});

	it('goes when the loaded answer says the document has no row any more', async () => {
		await returnToNotes();
		expect(ownerLabel()).toBe('Owner: Release planning');
		answerNotes!(null);
		await vi.waitFor(() => expect(ownerLabel()).toBeNull());
	});

	it('goes when the load fails, so its pickers do not stay waiting', async () => {
		await returnToNotes();
		expect(ownerLabel()).toBe('Owner: Release planning');
		failNotes!();
		await vi.waitFor(() => expect(ownerLabel()).toBeNull());
		expect(host.querySelectorAll('[data-ownership-row] button.item').length).toBe(0);
	});

	it('closes an owner picker opened on one tab when another tab\'s held load starts, and writes nothing', async () => {
		// Both tabs have shown a row, so both are remembered: the switch keeps the control mounted while the load is held.
		let holdOther = false; let release: (() => void) | null = null;
		api.fetchContent.mockImplementation(async (path: string) => {
			const id = path.includes('other') ? 'other' : 'notes';
			const answer = { text: texts[id], writable: true, artifact: { id }, freshness: null, ownership: ownedBy(`${id} owner`) };
			if (id === 'other' && holdOther) return new Promise(resolve => { release = () => resolve(answer); });
			return answer;
		});
		const props = $state({ target: notes });
		host = document.createElement('div'); document.body.appendChild(host);
		component = mount(DocViewer, { target: host, props });
		flushSync();
		await serverOpen('notes');
		await vi.waitFor(() => expect(ownerLabel()).toBe('Owner: notes owner'));
		props.target = other; flushSync();
		await serverOpen('other');
		await vi.waitFor(() => expect(ownerLabel()).toBe('Owner: other owner'));
		props.target = notes; flushSync();
		await vi.waitFor(() => expect(owner()?.disabled).toBe(false));
		owner()!.click();
		await vi.waitFor(() => expect(document.body.querySelector('.role-picker')).not.toBeNull());
		holdOther = true;
		props.target = other; flushSync();
		await vi.waitFor(() => expect(document.body.querySelector('.role-picker')).toBeNull());
		for (const button of host.querySelectorAll<HTMLButtonElement>('[data-ownership-row] button.item')) expect(button.disabled).toBe(true);
		expect(changeOwnership).not.toHaveBeenCalled();
		release?.();
	});
});
