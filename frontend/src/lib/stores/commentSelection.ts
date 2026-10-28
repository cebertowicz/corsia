import * as Y from 'yjs';
import { encodeUpdate, newClientId, textDigest } from './documentWire';
import type { LiveDocumentSession } from './liveDocumentSession';

export const SELECTION_LOST = 'The text you selected changed or was removed. Select the text again; your draft will carry over.';
export const SELECTION_RECONNECTED = "This draft isn't attached to any text. Select the text again; your draft will carry over.";
export interface CommentSelection {
	session: LiveDocumentSession;
	from: Y.RelativePosition;
	to: Y.RelativePosition;
	quote: string;
}
export interface CapturedComment {
	from: number;
	to: number;
	quote: string;
	clientId: string;
}
export function selectComment(session: LiveDocumentSession, from: number, to: number): CommentSelection {
	return { session, from: Y.createRelativePositionFromTypeIndex(session.text, from, 0),
		to: Y.createRelativePositionFromTypeIndex(session.text, to, -1), quote: session.text.toString().slice(from, to) };
}
export function selectionRange(selection: CommentSelection): { from: number; to: number } | null {
	const { session } = selection;
	const from = Y.createAbsolutePositionFromRelativePosition(selection.from, session.doc)?.index;
	const to = Y.createAbsolutePositionFromRelativePosition(selection.to, session.doc)?.index;
	return from !== undefined && to !== undefined && from < to && session.text.toString().slice(from, to) === selection.quote
		? { from, to } : null;
}
/** How long a capture keeps asking the server to track its range, counted from its first ask, on a document that keeps moving. */
export const RESERVE_BUDGET_MS = 3000;
/** After a refusal, how long a capture waits for the remote update the server already holds before it asks again. */
export const CATCH_UP_MS = 50;
const CHANGED = 'The document changed before your comment could be attached. Your draft is kept. Try again.';
const OFFLINE = 'You’re offline. Your comment draft is kept. Try again when connected.';
const UNANSWERED = 'Couldn’t attach your comment. Your draft is kept. Try again.';

/**
 * One correlated capture owned by the existing draft. It asks the server to track the selection under the
 * comment's own client id, which succeeds only when the server holds the state this browser hashed. A hash a
 * remote update interrupts is simply redone. After a refusal it waits for the next applied remote update (or
 * CATCH_UP_MS), so the browser has caught up, and asks again until RESERVE_BUDGET_MS runs out. Abort, refusal, or
 * an unanswered reservation gives the range back; abort also releases an in-progress hash.
 */
export function captureComment(selection: CommentSelection, seq: number, signal: AbortSignal): Promise<CapturedComment> {
	const { session } = selection, clientId = newClientId();
	return new Promise((resolve, reject) => {
		const started = performance.now();
		let done = false, requestId: string | null = null, sent: CapturedComment | null = null;
		let deadline: ReturnType<typeof setTimeout> | undefined;
		let stopFrame = () => {}, stopState = () => {}, stopWait = () => {};
		const finish = (result?: CapturedComment, error?: Error) => {
			if (done) return;
			done = true; clearTimeout(deadline); stopFrame(); stopState(); stopWait(); signal.removeEventListener('abort', abort);
			session.cancelReservationRequest(requestId);
			if (result) { resolve(result); return; }
			if (sent) session.releaseReservation(clientId);
			reject(error ?? new Error(CHANGED));
		};
		const abort = () => finish(undefined, new DOMException('Comment selection request cancelled', 'AbortError'));
		if (signal.aborted) { abort(); return; }
		signal.addEventListener('abort', abort, { once: true });
		stopFrame = session.onFrame(frame => {
			// No ask is out yet while hashing: a refusal without an id (an earlier capture's release) is not ours.
			if (requestId === null || frame.request_id !== requestId) return;
			if (frame.type === 'doc_error') {
				finish(undefined, new Error((frame.kind === 'refused' || frame.kind === 'store_refused') && frame.message ? frame.message : CHANGED));
				return;
			}
			if (frame.artifact_id !== session.artifactId || frame.store_id !== session.storeId || frame.type !== 'doc_positions' || frame.seq !== seq || !sent) return;
			if (frame.reserved) finish(sent); else again(true);
		});
		stopState = session.subscribe(() => {
			if (session.connection !== 'connected') finish(undefined, new Error(OFFLINE));
		});
		if (done) { stopState(); return; }
		// An ask still out means the server never answered; otherwise the budget ran out on a document that kept moving.
		deadline = setTimeout(() => finish(undefined, sent ? new Error(UNANSWERED) : undefined), RESERVE_BUDGET_MS);
		void ask();
		function again(refused: boolean) {
			sent = null;
			if (performance.now() - started >= RESERVE_BUDGET_MS) finish(); else if (refused) catchUp(); else void ask();
		}
		function catchUp() {
			const generation = session.textGeneration;
			let stop = () => {}, timer: ReturnType<typeof setTimeout> | undefined;
			const go = () => { clearTimeout(timer); stop(); stopWait = () => {}; if (!done) void ask(); };
			stopWait = () => { clearTimeout(timer); stop(); };
			stop = session.subscribe(() => { if (session.textGeneration !== generation) go(); });
			timer = setTimeout(go, CATCH_UP_MS);
		}
		async function ask() {
			const generation = session.textGeneration, before = session.durability.snapshot(), text = session.text.toString();
			try {
				const digest = await textDigest(text), after = session.durability.snapshot();
				if (done) return;
				if (generation !== session.textGeneration || before.localGeneration !== after.localGeneration
					|| before.ackGeneration !== after.ackGeneration) { again(false); return; }
				if (!session.durability.clean) { finish(undefined, new Error('Live updates are still settling. Your comment draft is kept. Try again.')); return; }
				const range = selectionRange(selection);
				if (!range) { finish(undefined, new Error(SELECTION_LOST)); return; }
				const vector = encodeUpdate(Y.encodeStateVector(session.doc));
				sent = { ...range, quote: selection.quote, clientId };
				requestId = session.requestPositions(seq, false, { client_id: clientId, from_off: range.from, to_off: range.to,
					vector, digest, text_length_utf16: text.length });
				if (!requestId) finish(undefined, new Error(OFFLINE));
			} catch (error) { finish(undefined, error instanceof Error ? error : new Error('Could not check the selection.')); }
		}
	});
}
