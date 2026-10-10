import { createHash } from 'node:crypto';
import { afterEach, expect, it, vi } from 'vitest';
import * as Y from 'yjs';
import { LiveDocumentSession } from './liveDocumentSession';
import { captureComment, selectComment, RESERVE_BUDGET_MS, SELECTION_LOST } from './commentSelection';
import { encodeUpdate, type DocumentFrame } from './documentWire';

const documents: Y.Doc[] = [], sessions: LiveDocumentSession[] = [];
afterEach(() => { sessions.splice(0).forEach(session => session.destroy()); documents.splice(0).forEach(doc => doc.destroy()); vi.restoreAllMocks(); });
function opened() {
	const server = new Y.Doc(); server.clientID = 1; server.getText('text').insert(0, 'alpha beta gamma');
	const doc = new Y.Doc(), sent: Record<string, unknown>[] = [];
	const session = new LiveDocumentSession({ artifactId: 'artifact', storeId: 'store', doc, writable: true,
		send: frame => { sent.push(frame); return true; } });
	session.opened(Y.encodeStateAsUpdate(server), Y.encodeStateVector(server));
	session.handle({ type: 'doc_ack', artifact_id: 'artifact', store_id: 'store', message_ids: sent.map(frame => String(frame.message_id)) });
	documents.push(server, doc); sessions.push(session);
	const asks = () => sent.filter(frame => frame.type === 'doc_positions');
	const reserve = (index = asks().length - 1) => asks()[index].reserve as Record<string, any>;
	return { server, session, sent, selection: selectComment(session, 6, 10), asks, reserve,
		released: () => sent.filter(frame => frame.type === 'doc_release'),
		asked: (count: number) => vi.waitFor(() => expect(asks().length).toBe(count)),
		answer: (fields: Partial<DocumentFrame> = {}) => session.handle({ type: 'doc_positions', artifact_id: 'artifact', store_id: 'store',
			request_id: String(asks().at(-1)!.request_id), seq: 1, reserved: true, ...fields }),
		remote: (edit: (text: Y.Text) => void) => { const vector = Y.encodeStateVector(server); edit(server.getText('text'));
			session.handle({ type: 'doc_update', artifact_id: 'artifact', store_id: 'store', update: encodeUpdate(Y.encodeStateAsUpdate(server, vector)) }); },
	};
}
it('asks for the selection under a fresh client id with the browser\'s state, and takes only its own answer', async () => {
	const value = opened(), received = vi.fn();
	const request = captureComment(value.selection, 1, new AbortController().signal).then(received);
	await value.asked(1);
	expect(value.reserve()).toEqual({ client_id: expect.any(String), from_off: 6, to_off: 10, vector: encodeUpdate(Y.encodeStateVector(value.session.doc)),
		digest: createHash('sha256').update('alpha beta gamma').digest('hex').slice(0, 16), text_length_utf16: 16 });
	for (const fields of [{ artifact_id: 'other' }, { store_id: 'other' }, { request_id: 'other' }, { seq: 2 }]) value.answer(fields);
	await Promise.resolve();
	expect(received).not.toHaveBeenCalled();
	value.answer(); await request;
	expect(received).toHaveBeenCalledWith(expect.objectContaining({ from: 6, to: 10, quote: 'beta', clientId: value.reserve().client_id }));
	expect(value.released()).toEqual([]);
});
it('asks again after a refusal and succeeds once the server holds the state it hashed, under the same id', async () => {
	const value = opened(), request = captureComment(value.selection, 1, new AbortController().signal);
	await value.asked(1);
	const first = value.reserve(0).client_id;
	value.answer({ reserved: false });
	await value.asked(2);
	expect(value.asks()[1].request_id).not.toBe(value.asks()[0].request_id);
	value.answer();
	await expect(request).resolves.toMatchObject({ quote: 'beta', clientId: first });
	expect(value.released()).toEqual([]);
});
it('shows the document-changed message only once the time budget runs out, and holds nothing', async () => {
	const value = opened(), now = vi.spyOn(performance, 'now');
	now.mockReturnValue(1000);
	const request = captureComment(value.selection, 1, new AbortController().signal);
	const refused = expect(request).rejects.toThrow('document changed');
	await value.asked(1);
	now.mockReturnValue(1000 + RESERVE_BUDGET_MS - 1); value.answer({ reserved: false });
	await value.asked(2);
	now.mockReturnValue(1000 + RESERVE_BUDGET_MS); value.answer({ reserved: false });
	await refused;
	expect(value.asks()).toHaveLength(2);
	expect(value.released()).toEqual([]);
});
it('waits for the next applied remote update, or 50 ms, after a refusal before it asks again', async () => {
	const value = opened(), request = captureComment(value.selection, 1, new AbortController().signal);
	await value.asked(1);
	value.answer({ reserved: false });
	for (let turn = 0; turn < 5; turn++) await Promise.resolve();
	expect(value.asks()).toHaveLength(1);
	value.remote(text => text.insert(0, 'X '));
	await value.asked(2);
	expect(value.reserve().text_length_utf16).toBe(18);
	value.answer({ reserved: false });
	for (let turn = 0; turn < 5; turn++) await Promise.resolve();
	expect(value.asks()).toHaveLength(2);
	await value.asked(3);
	value.answer();
	await expect(request).resolves.toMatchObject({ quote: 'beta', from: 8 });
});
it('is not spent by hashes a remote update interrupts', async () => {
	const value = opened(), original = crypto.subtle.digest.bind(crypto.subtle), releases: (() => void)[] = [];
	vi.spyOn(crypto.subtle, 'digest').mockImplementation(async (algorithm, data) => {
		if (releases.length < 5) await new Promise<void>(resolve => { releases.push(resolve); });
		return original(algorithm, data);
	});
	const request = captureComment(value.selection, 1, new AbortController().signal);
	for (let round = 1; round <= 5; round++) {
		await vi.waitFor(() => expect(releases).toHaveLength(round));
		value.remote(text => text.insert(0, 'X'));
		releases[round - 1]();
	}
	await value.asked(1);
	expect(value.reserve().text_length_utf16).toBe(21);
	value.answer();
	await expect(request).resolves.toMatchObject({ quote: 'beta', from: 11 });
});
it('succeeds under remote typing every 50 ms for 2 s once a round trip finds the document still', async () => {
	const value = opened(), answered = new Set<string>();
	const digestOf = () => createHash('sha256').update(value.server.getText('text').toString()).digest('hex').slice(0, 16);
	const typing = setInterval(() => value.remote(text => text.insert(0, 'x')), 50);
	const stopTyping = setTimeout(() => clearInterval(typing), 2000);
	// The server answers each ask 60 ms after it arrives, from the text it holds by then, so every ask misses while typing continues.
	const serve = setInterval(() => value.asks().forEach(ask => {
		const id = String(ask.request_id);
		if (answered.has(id)) return;
		answered.add(id);
		const hashed = (ask.reserve as Record<string, string>).digest;
		setTimeout(() => value.answer({ request_id: id, reserved: hashed === digestOf() }), 60);
	}), 5);
	try {
		await expect(captureComment(value.selection, 1, new AbortController().signal)).resolves.toMatchObject({ quote: 'beta' });
		expect(value.asks().length).toBeGreaterThan(3);
	} finally { clearInterval(typing); clearTimeout(stopTyping); clearInterval(serve); }
}, 10_000);
it('gives the range back when the server refuses the request or the capture is aborted after asking', async () => {
	const value = opened(), abort = new AbortController();
	const refused = expect(captureComment(value.selection, 1, new AbortController().signal)).rejects.toThrow('refused for test');
	await value.asked(1);
	value.session.handle({ type: 'doc_error', artifact_id: 'artifact', store_id: 'store', request_id: String(value.asks()[0].request_id), message: 'refused for test' });
	await refused;
	expect(value.released()).toEqual([expect.objectContaining({ client_id: value.reserve(0).client_id })]);
	const cancelled = expect(captureComment(value.selection, 1, abort.signal)).rejects.toMatchObject({ name: 'AbortError' });
	await value.asked(2); abort.abort(); await cancelled;
	expect(value.released()).toHaveLength(2);
	expect(value.released()[1]).toMatchObject({ client_id: value.reserve(1).client_id });
});
it.each(['remote', 'ack'] as const)('hashes again when %s moved the document during hashing and reserves the new state', async kind => {
	const value = opened(), original = crypto.subtle.digest.bind(crypto.subtle);
	let release!: () => void;
	vi.spyOn(crypto.subtle, 'digest').mockImplementationOnce(async (algorithm, data) => {
		await new Promise<void>(resolve => { release = resolve; }); return original(algorithm, data);
	});
	const request = captureComment(value.selection, 1, new AbortController().signal);
	await vi.waitFor(() => expect(release).toBeTypeOf('function'));
	if (kind === 'remote') value.remote(text => text.insert(0, 'X '));
	if (kind === 'ack') value.session.handle({ type: 'doc_ack', artifact_id: 'artifact', store_id: 'store', message_ids: [] });
	release(); await value.asked(1);
	expect(value.reserve().text_length_utf16).toBe(kind === 'remote' ? 18 : 16);
	value.answer();
	await expect(request).resolves.toMatchObject({ quote: 'beta', from: kind === 'remote' ? 8 : 6 });
});
it('refuses a local edit made during hashing as still settling', async () => {
	const value = opened(), original = crypto.subtle.digest.bind(crypto.subtle);
	let release!: () => void;
	vi.spyOn(crypto.subtle, 'digest').mockImplementationOnce(async (algorithm, data) => {
		await new Promise<void>(resolve => { release = resolve; }); return original(algorithm, data);
	});
	const request = captureComment(value.selection, 1, new AbortController().signal);
	const refused = expect(request).rejects.toThrow('still settling');
	await vi.waitFor(() => expect(release).toBeTypeOf('function'));
	value.session.text.insert(0, 'X ');
	release(); await refused;
	expect(value.asks()).toHaveLength(0);
});
it.each(['dirty', 'disconnected', 'closed'] as const)('refuses %s without asking and holds nothing', async kind => {
	const value = opened();
	if (kind === 'dirty') value.session.text.insert(0, 'X ');
	const request = captureComment(value.selection, 1, new AbortController().signal);
	const refused = expect(request).rejects.toThrow(kind === 'dirty' ? 'still settling' : 'offline');
	if (kind === 'disconnected') value.session.disconnected();
	if (kind === 'closed') value.session.destroy();
	await refused;
	expect(value.asks()).toHaveLength(0); expect(value.released()).toEqual([]);
});
it('aborts during hashing, asks for nothing and ignores the released hash', async () => {
	const value = opened(), abort = new AbortController(), original = crypto.subtle.digest.bind(crypto.subtle);
	let release!: () => void;
	vi.spyOn(crypto.subtle, 'digest').mockImplementationOnce(async (algorithm, data) => {
		await new Promise<void>(resolve => { release = resolve; }); return original(algorithm, data);
	});
	const request = captureComment(value.selection, 1, abort.signal);
	const refused = expect(request).rejects.toMatchObject({ name: 'AbortError' });
	await vi.waitFor(() => expect(release).toBeTypeOf('function'));
	abort.abort(); await refused; release(); await Promise.resolve(); await Promise.resolve();
	expect(value.asks()).toHaveLength(0); expect(value.released()).toEqual([]);
});
it('rejects equal text with deleted and reinserted character identity without asking', async () => {
	const value = opened(), request = captureComment(value.selection, 1, new AbortController().signal);
	const refused = expect(request).rejects.toThrow(SELECTION_LOST);
	value.remote(text => { text.delete(6, 4); text.insert(6, 'beta'); });
	await refused;
	expect(value.asks()).toHaveLength(0);
});
