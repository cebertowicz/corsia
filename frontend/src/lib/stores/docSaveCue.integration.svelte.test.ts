import { flushSync } from 'svelte';
import * as Y from 'yjs';
import { afterEach, beforeEach, expect, it, vi } from 'vitest';
import { createDocSaveState } from '$lib/docViewerSaveState.svelte';
import { clearDraft, existingDraft, seedDraft, setDraftText } from './docSaveQueue';
import { liveDocumentBindings } from './liveDocuments';
import { LiveDocumentSession } from './liveDocumentSession';

const mocks = vi.hoisted(() => ({ write: vi.fn(), toast: vi.fn() }));
vi.mock('$lib/api/docs', () => ({ writeContent: mocks.write }));
vi.mock('./docComments', () => ({ flushPositions: vi.fn() }));
vi.mock('./toast', () => ({ toasts: { show: mocks.toast } }));
const target = { instanceId: 'cue-a', path: '/cue.md', name: 'cue.md', artifactId: null, projectId: null, threadId: null };
const other = { ...target, instanceId: 'cue-b' };
let dispose: () => void;
let session: LiveDocumentSession | undefined;
let state: ReturnType<typeof createDocSaveState>;
let current = $state(target);

function deferred() {
	let resolve!: (value: any) => void;
	let reject!: (value: Error) => void;
	const promise = new Promise<any>((yes, no) => { resolve = yes; reject = no; });
	return { promise, resolve, reject };
}
async function advance(ms: number) { await vi.advanceTimersByTimeAsync(ms); flushSync(); }
async function settle(write: ReturnType<typeof deferred>) { write.resolve({ saved: true }); await advance(0); }
function live() {
	const doc = new Y.Doc();
	doc.getText('text').insert(0, 'base');
	session = new LiveDocumentSession({ doc, artifactId: 'artifact', storeId: 'store', writable: true, send: () => true });
	session.connection = 'connected';
	liveDocumentBindings.set({ [target.instanceId]: session });
	return session;
}
beforeEach(() => {
	vi.useFakeTimers();
	mocks.write.mockReset().mockResolvedValue({ saved: true });
	current = target;
	seedDraft(target, 'base');
	dispose = $effect.root(() => { state = createDocSaveState(() => current); });
	flushSync();
});
afterEach(() => {
	dispose();
	clearDraft(target); clearDraft(other);
	liveDocumentBindings.set({});
	session?.destroy(); session?.doc.destroy(); session = undefined;
	vi.restoreAllMocks();
	vi.useRealTimers();
});

it('real queue: idle and typing during the 700ms debounce stay quiet', async () => {
	expect(state.status).toBe('idle');
	setDraftText(target, 'changed');
	await advance(699);
	expect(state.status).toBe('idle');
	expect(mocks.write).not.toHaveBeenCalled();
});
it('real queue: a fast save under 400ms never shows a cue', async () => {
	setDraftText(target, 'changed');
	await advance(700);
	expect(existingDraft(target)?.savedText).toBe('changed');
	expect(state.status).toBe('idle');
});
it('real queue: a slow write ends in Saved and then idle', async () => {
	const slow = deferred(); mocks.write.mockReturnValueOnce(slow.promise);
	setDraftText(target, 'changed');
	await advance(700);
	expect(state.status).toBe('idle');
	await advance(399); expect(state.status).toBe('idle');
	await advance(1); expect(state.status).toBe('saving');
	await settle(slow); expect(state.status).toBe('saved');
	await advance(1399); expect(state.status).toBe('saved');
	await advance(1); expect(state.status).toBe('idle');
});
it('real queue: keystrokes and chained resaves extend one episode', async () => {
	const first = deferred(), second = deferred();
	mocks.write.mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
	setDraftText(target, 'first'); await advance(1100);
	expect(state.status).toBe('saving');
	setDraftText(target, 'second'); flushSync();
	expect(state.status).toBe('saving');
	await settle(first);
	expect(mocks.write).toHaveBeenCalledTimes(2);
	expect(state.status).toBe('saving');
	await settle(second); expect(state.status).toBe('saved');
});
it.each([30, 450])('real queue: disconnect after %ims suspends the cue and a 30ms reconnect flush confirms', async (elapsed) => {
	const connection = live(); const interrupted = deferred(), recovered = deferred();
	vi.spyOn(connection, 'flush').mockReturnValueOnce(interrupted.promise).mockReturnValueOnce(recovered.promise);
	vi.spyOn(connection, 'durability', 'get').mockReturnValue({ clean: false, unwritten: true } as any);
	setDraftText(target, 'offline'); await advance(700 + elapsed);
	connection.disconnected(); interrupted.reject(new Error('offline')); await advance(0);
	expect(state.status).toBe('idle');
	expect(existingDraft(target)?.savedText).toBe('base');
	await advance(2000); expect(state.status).toBe('idle');
	connection.connection = 'connected';
	connection.handle({ type: 'doc_opened', artifact_id: 'artifact', store_id: 'store', pending: false, held: null });
	await advance(30);
	vi.spyOn(connection, 'durability', 'get').mockReturnValue({ clean: true, unwritten: false } as any);
	await settle(recovered); expect(state.status).toBe('saved');
	await advance(1400); expect(state.status).toBe('idle');
});
it('real queue: text typed offline owes confirmation after reconnect', async () => {
	const connection = live(); connection.disconnected();
	vi.spyOn(connection, 'durability', 'get').mockReturnValue({ clean: false, unwritten: true } as any);
	setDraftText(target, 'offline'); await advance(700);
	expect(state.status).toBe('idle');
	const recovered = deferred(); vi.spyOn(connection, 'flush').mockReturnValueOnce(recovered.promise);
	connection.connection = 'connected';
	connection.handle({ type: 'doc_opened', artifact_id: 'artifact', store_id: 'store', pending: false, held: null });
	await advance(30);
	vi.spyOn(connection, 'durability', 'get').mockReturnValue({ clean: true, unwritten: false } as any);
	await settle(recovered); expect(state.status).toBe('saved');
});
it('real queue: an unacked transaction keeps the draft unclean and queues a resave without Saved', async () => {
	const connection = live(); const first = deferred(), second = deferred();
	vi.spyOn(connection, 'flush').mockReturnValueOnce(first.promise).mockReturnValueOnce(second.promise);
	vi.spyOn(connection, 'durability', 'get').mockReturnValue({ clean: false, unwritten: true } as any);
	setDraftText(target, 'unacked'); await advance(1100);
	await settle(first);
	expect(existingDraft(target)?.savedText).toBe('base');
	expect(state.status).toBe('saving');
	expect(connection.flush).toHaveBeenCalledTimes(2);
	vi.spyOn(connection, 'durability', 'get').mockReturnValue({ clean: true, unwritten: false } as any);
	await settle(second); expect(state.status).toBe('saved');
});
it('real queue: error ends without Saved; editing and retry start a fresh clock', async () => {
	const broken = deferred(), retry = deferred();
	mocks.write.mockReturnValueOnce(broken.promise).mockReturnValueOnce(retry.promise);
	vi.spyOn(console, 'error').mockImplementation(() => {});
	setDraftText(target, 'changed'); await advance(1100);
	broken.reject(new Error('failed')); await advance(0); expect(state.status).toBe('idle');
	expect(state.error).toContain("Couldn't save");
	setDraftText(target, 'retry'); await advance(700);
	expect(state.status).toBe('idle');
	await advance(400); expect(state.status).toBe('saving');
	await settle(retry); expect(state.status).toBe('saved');
});
it('real queue: tab switches keep each draft clock and do not replay an offscreen save', async () => {
	const slow = deferred(); mocks.write.mockReturnValueOnce(slow.promise);
	seedDraft(other, 'base'); setDraftText(target, 'changed'); await advance(900);
	current = other; flushSync(); await advance(200); expect(state.status).toBe('idle');
	current = target; flushSync(); expect(state.status).toBe('saving');
	current = other; flushSync(); await settle(slow);
	current = target; flushSync(); expect(state.status).toBe('idle');
});
it('real queue: a first look at an already saved draft stays quiet', async () => {
	current = other; flushSync(); setDraftText(target, 'changed'); await advance(700);
	current = target; flushSync(); expect(state.status).toBe('idle');
});
