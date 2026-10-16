import * as Y from 'yjs';
import { DocOpenError, docOpenRefusal, liveUnavailable, sessionRefusal, type DocOpenRefusal, type LiveUnavailable } from '$lib/docOpenRefusal';
import { get, writable } from 'svelte/store';
import type { DocViewerTarget } from '$lib/docViewerTarget';
import { connectionState } from './connection';
import { on } from '$lib/ws/events';
import { ws } from '$lib/ws/state';
import { DeviceDocument } from './deviceDocument';
import { readDeviceRecovery, type DeviceRecovery } from './deviceRecovery';
import { decodeUpdate, type DocumentFrame } from './documentWire';
import { LiveDocumentSession } from './liveDocumentSession';
import { LiveOperationError } from './documentFailures';

interface Binding { target: DocViewerTarget; session: LiveDocumentSession | null }
interface Opening {
	binding: Binding;
	controller: AbortController;
	/** Existing-session reconnects must reopen the same artifact, never replace this binding. */
	expectedArtifactId?: string;
	resolve: (outcome: LiveOpenOutcome) => void;
	reject: (error: Error) => void;
	/** A first open waits for the socket until this time; reconnect opens have none. */
	deadline?: number;
	timer?: ReturnType<typeof setTimeout>;
}
/**
 * How long a first open waits for the socket before it is refused as not
 * connected. It equals the socket's longest reconnect backoff (30s), so one
 * full backoff step, and the first five steps (about 20s) together, fit
 * inside it; a tab restored at page load therefore opens on the first connect.
 */
export const LIVE_OPEN_WAIT_MS = 30_000;
const NOT_CONNECTED: DocOpenRefusal = {
	code: 'not_connected',
	title: 'Live editing is not connected.',
	message: 'CORSIA could not reach the server to open this document. Text in this tab is kept. It reopens when the connection returns.',
};
/** A live session, or a non-live answer with the reason live editing is unavailable, if the server gave one. */
export type LiveOpenOutcome = LiveDocumentSession | LiveUnavailable | null;
const bindings = new Map<string, Binding>();
const openings = new Map<string, Opening>();
const waiting = new Set<Opening>();
const sessions = new Map<string, LiveDocumentSession>();
export const liveDocumentBindings = writable<Record<string, LiveDocumentSession>>({});
let receive = Promise.resolve();
let startCount = 0;
let stops: (() => void)[] = [];

function publish(): void {
	liveDocumentBindings.set(Object.fromEntries([...bindings].flatMap(([id, binding]) => binding.session ? [[id, binding.session]] : [])));
}

function send(frame: Record<string, unknown>): boolean {
	if (get(connectionState) !== 'connected' || ws?.readyState !== WebSocket.OPEN) return false;
	ws.send(JSON.stringify(frame));
	return true;
}

function requestOpen(opening: Opening): void {
	const id = crypto.randomUUID(), { target } = opening.binding;
	const frame = { type: 'doc_open', request_id: id, path: target.path, project_id: target.projectId, thread_id: target.threadId,
		...(opening.expectedArtifactId ? { expected_artifact_id: opening.expectedArtifactId } : {}) };
	if (send(frame)) {
		clearTimeout(opening.timer);
		openings.set(id, opening);
	} else if (opening.deadline === undefined) {
		opening.reject(new Error('Disconnected; reopen when the connection returns'));
	} else {
		awaitConnection(opening);
	}
}

/** Holds a first open until the socket connects, refusing it as not connected at its deadline. */
function awaitConnection(opening: Opening): void {
	waiting.add(opening);
	clearTimeout(opening.timer);
	opening.timer = setTimeout(() => {
		waiting.delete(opening);
		opening.reject(new DocOpenError(NOT_CONNECTED));
	}, Math.max(0, opening.deadline! - Date.now()));
}

export function openLiveDocument(target: DocViewerTarget): Promise<LiveOpenOutcome> {
	const existing = bindings.get(target.instanceId);
	if (existing?.session) return Promise.resolve(existing.session);
	const binding = existing ?? { target, session: null };
	bindings.set(target.instanceId, binding);
	return new Promise((resolve, reject) => {
		const opening: Opening = { binding, controller: new AbortController(), deadline: Date.now() + LIVE_OPEN_WAIT_MS,
			resolve: value => { clearTimeout(opening.timer); resolve(value); },
			reject: error => { clearTimeout(opening.timer); reject(error); } };
		requestOpen(opening);
	});
}

function destroySession(session: LiveDocumentSession): void {
	session.destroy();
	void session.device?.destroy();
	session.doc.destroy();
}

function whileOpening<T>(loading: Promise<T>, signal: AbortSignal): Promise<T> {
	return new Promise((resolve, reject) => {
		const closed = () => reject(new Error('The document closed while opening'));
		if (signal.aborted) { closed(); return; }
		signal.addEventListener('abort', closed, { once: true });
		loading.then(value => {
			signal.removeEventListener('abort', closed);
			resolve(value);
		}, error => {
			signal.removeEventListener('abort', closed);
			reject(error);
		});
	});
}

async function freshSession(frame: DocumentFrame, signal: AbortSignal, previous?: LiveDocumentSession): Promise<LiveDocumentSession> {
	const doc = new Y.Doc();
	const session = new LiveDocumentSession({ artifactId: frame.artifact_id!, storeId: frame.store_id!, doc,
		writable: frame.writable !== false, send });
	let device: DeviceDocument | undefined;
	try {
		device = new DeviceDocument(session.artifactId, session.storeId, doc);
		await whileOpening(device.ready, signal);
		let recovery: DeviceRecovery[] = [];
		let error = '';
		try { recovery = await whileOpening(readDeviceRecovery(session.artifactId, session.storeId), signal); }
		catch (reason) {
			if (signal.aborted) throw reason;
			error = reason instanceof Error ? reason.message : 'Device recovery is unavailable';
		}
		if (previous && !previous.durability.clean) {
			recovery = recovery.filter(copy => copy.storeId !== previous.storeId);
			recovery.unshift({ storeId: previous.storeId, text: previous.text.toString() });
		}
		session.attachDevice(device, recovery, error);
		return session;
	} catch (error) {
		void device?.destroy();
		session.destroy();
		doc.destroy();
		throw error;
	}
}

async function opened(frame: DocumentFrame): Promise<void> {
	const request = openings.get(frame.request_id ?? '');
	if (!request) return;
	try {
		if (bindings.get(request.binding.target.instanceId) !== request.binding) throw new Error('The document closed while opening');
		// A non-live answer never opens, reads or deletes the device copy.
		if (!frame.live) { request.resolve(liveUnavailable(frame)); return; }
		if (!frame.artifact_id || !frame.store_id || !frame.update || !frame.vector) {
			throw new Error('The document sync response is incomplete');
		}
		if (request.expectedArtifactId && frame.artifact_id !== request.expectedArtifactId) {
			request.resolve(null);
			return;
		}
		const update = decodeUpdate(frame.update), vector = decodeUpdate(frame.vector);
		let session = sessions.get(frame.artifact_id);
		if (!session || session.storeId !== frame.store_id) {
			const previous = session;
			session = await freshSession(frame, request.controller.signal, previous);
			if (openings.get(frame.request_id!) !== request || bindings.get(request.binding.target.instanceId) !== request.binding) {
				destroySession(session);
				throw new Error('The document closed while opening');
			}
			if (previous) {
				for (const binding of bindings.values()) if (binding.session === previous) binding.session = session;
				destroySession(previous);
			}
			sessions.set(frame.artifact_id, session);
		}
		request.binding.session = session;
		session.opened(update, vector, frame.held ?? null, frame.writable !== false);
		session.admitFailures(frame);
		session.handle(frame);
		publish();
		request.resolve(session);
	} catch (error) {
		// A closed opening is already settled; one cut by a disconnect waits under a new request.
		if (openings.get(frame.request_id!) !== request) return;
		request.reject(error instanceof Error ? error : new Error('Could not open live document'));
	} finally {
		openings.delete(frame.request_id!);
	}
}

async function handle(frame: DocumentFrame): Promise<void> {
	if (frame.type === 'doc_opened') { await opened(frame); return; }
	const opening = openings.get(frame.request_id ?? '');
	if (frame.type === 'doc_error' && opening) {
		openings.delete(frame.request_id!);
		const refusal = docOpenRefusal(frame);
		opening.reject(refusal ? new DocOpenError(refusal) : new LiveOperationError(frame.kind, frame.message ?? 'Could not open live document'));
		return;
	}
	if (frame.artifact_id) sessions.get(frame.artifact_id)?.handle(frame);
}

function connectionChanged(state: string): void {
	if (state !== 'connected') {
		for (const session of sessions.values()) session.disconnected();
		const interrupted = [...openings.values()];
		openings.clear();
		for (const opening of interrupted) {
			opening.controller.abort();
			if (opening.deadline === undefined) {
				opening.reject(new Error('Disconnected; your text stays in this tab'));
				continue;
			}
			opening.controller = new AbortController();
			awaitConnection(opening);
		}
		return;
	}
	const queued = [...waiting];
	waiting.clear();
	for (const opening of queued) requestOpen(opening);
	for (const session of sessions.values()) reopenLiveSession(session);
}

/**
 * Re-runs an existing session's own open, on reconnect and from its refusal's Retry. It never
 * takes the non-live path: a non-live answer or a typed refusal refuses the session in place.
 */
export function reopenLiveSession(session: LiveDocumentSession): void {
	const binding = [...bindings.values()].find(binding => binding.session === session);
	if (!binding) return;
	session.reconnecting();
	const opening: Opening = { binding, controller: new AbortController(), expectedArtifactId: session.artifactId,
		resolve: outcome => {
			if (!outcome || typeof outcome === 'string') session.refuse(sessionRefusal(outcome));
		},
		reject: error => {
			const refusal = docOpenRefusal(error);
			if (refusal) { session.refuse(refusal); return; }
			session.disconnected();
			console.error('document reconnect failed', error);
		} };
	requestOpen(opening);
}

export function closeLiveDocument(instanceId: string): void {
	const binding = bindings.get(instanceId);
	for (const [id, opening] of openings) if (opening.binding === binding) {
		openings.delete(id);
		opening.controller.abort();
		opening.reject(new Error('The document closed while opening'));
	}
	for (const opening of waiting) if (opening.binding === binding) {
		waiting.delete(opening);
		opening.reject(new Error('The document closed while opening'));
	}
	bindings.delete(instanceId);
	publish();
	const session = binding?.session;
	if (!session || [...bindings.values()].some(binding => binding.session === session)) return;
	session.close();
	destroySession(session);
	sessions.delete(session.artifactId);
}

function leaving(event: BeforeUnloadEvent): void {
	for (const session of sessions.values()) session.sync();
	if ([...sessions.values()].some(session => session.leavePrompt)) {
		event.preventDefault();
		event.returnValue = '';
	}
}

export function startLiveDocuments(): () => void {
	if (++startCount === 1) {
		stops = ['doc_opened', 'doc_update', 'doc_ack', 'doc_received', 'doc_saved', 'doc_error', 'doc_cleared', 'doc_positions', 'doc_pending'].map(type => on(type, frame => {
			receive = receive.then(() => handle(frame)).catch(error => console.error('document sync failed', error));
		}));
		stops.push(connectionState.subscribe(connectionChanged));
		window.addEventListener('beforeunload', leaving);
	}
	return () => {
		if (--startCount !== 0) return;
		for (const stop of stops) stop();
		stops = [];
		window.removeEventListener('beforeunload', leaving);
		for (const instanceId of [...bindings.keys()]) closeLiveDocument(instanceId);
	};
}
