import {
	onRecordedDocActivity,
	prunePresence,
	recordDocActivity,
	takePendingArrivals,
	type ArrivalQueue,
	type DocActivityFrame,
} from '$lib/docPresence';
import { createArrivalLane, type DocPresenceSurface, type RuntimeArrival } from '$lib/docPresenceLane';
import { holdCommentArrival, holdCommentCall, releaseCommentArrival, releaseCommentArrivals } from '$lib/stores/docCommentArrivals';
import { loadComments, onCommentsReloaded, type DocComment } from '$lib/stores/docComments';
import { connectionState, type ConnectionState } from '$lib/stores/connection';
import { on } from '$lib/ws/events';

export type { DocPresenceSurface, PreparedPresenceArrival } from '$lib/docPresenceLane';

/** How long a comment named by a change frame is held while its activity frame is awaited. */
const MISSING_FRAME_WAIT_MS = 5_000;

/**
 * The app socket, which carries every change and activity frame; frames sent while it is down never come back.
 * Every document is in the lost-frames window while it is down; once it is back, only documents a surface has open.
 */
let socket: 'unknown' | 'up' | 'down' = 'unknown';
/** Counts reconnects, so a reload from before the latest one cannot close the window that one opened. */
let reconnects = 0;

class ArtifactArrivalCoordinator {
	private readonly surfaces = new Map<string, DocPresenceSurface>();
	private readonly queues = new Map<string, ArrivalQueue<RuntimeArrival>>();
	/** Comments held on their change frame, each with the timer that ends the wait for its activity frame. */
	private readonly expected = new Map<string, ReturnType<typeof setTimeout>>();
	/** Comments released because no activity frame came; a late frame must not pull them back out. */
	private readonly missed = new Set<string>();
	/** Comments a lane has taken, kept after they show: a comment arrives once, so a later change or activity frame leaves it alone. */
	private readonly staged = new Set<string>();
	/** Back on the socket after a drop, no load has yet found what it missed: its reconnect reload waits for the text, is running, or failed. */
	private lostFrames: 'due' | 'reloading' | 'failed' | null = null;

	constructor(private readonly artifactId: string) {}

	attach(surfaceId: string, surface: DocPresenceSurface): () => void {
		this.surfaces.set(surfaceId, surface);
		void this.wake();
		return () => {
			this.surfaces.delete(surfaceId);
			if (this.surfaces.size) return;
			// Closed: a later open shows its comments as if there had been no drop.
			this.lostFrames = null;
			this.releaseAll();
		};
	}

	/** The socket is back: an open document reloads its comments, and looks for suggestions the drop missed until a load succeeds. */
	reloadAfterDrop(): void {
		if (!this.surfaces.size) return;
		this.lostFrames = 'due';
		this.reloadWhenTextCurrent();
	}

	/** The reconnect reload waits until the viewed document's live text is back, so the suggestions it finds can type. */
	private reloadWhenTextCurrent(): void {
		if (this.lostFrames !== 'due' || !(this.activeSurface()?.textCurrent?.() ?? true)) return;
		const drop = reconnects;
		this.lostFrames = 'reloading';
		loadComments(this.artifactId).then(
			() => { if (reconnects === drop && this.lostFrames === 'reloading') this.lostFrames = null; },
			() => { if (reconnects === drop && this.lostFrames === 'reloading') this.lostFrames = 'failed'; },
		);
	}

	/** A load succeeded and found `unseen`, rows this canvas never showed. Only frames lost to a drop stay missing; on a live socket their own frames are on their way. */
	reloaded(unseen: DocComment[]): void {
		if (socket !== 'down' && !this.lostFrames) return;
		this.arriveUnseen(unseen);
		// After a failed reconnect reload, the next load to succeed has found what the drop missed.
		if (socket === 'up' && this.lostFrames === 'failed') this.lostFrames = null;
	}

	/** A change frame named a new agent comment: hold it before any reload can show it, until its lane takes over. */
	expect(commentId: string): void {
		if (this.staged.has(commentId) || this.expected.has(commentId) || this.missed.has(commentId)) return;
		if (!this.activeSurface() || this.reducedMotion()) return;
		holdCommentArrival(this.artifactId, commentId);
		this.expected.set(commentId, setTimeout(() => {
			this.expected.delete(commentId);
			this.missed.add(commentId);
			releaseCommentArrival(this.artifactId, commentId);
		}, MISSING_FRAME_WAIT_MS));
	}

	/** Open agent suggestions whose frames were lost: each takes its agent's lane as its activity frame would have. */
	private arriveUnseen(rows: DocComment[]): void {
		if (!this.activeSurface() || this.reducedMotion()) return;
		for (const row of rows) {
			if (row.authorKind !== 'agent' || !row.authorThreadId || !row.createdAt || row.replacement == null || row.resolved || row.decision) continue;
			this.enqueue({
				type: 'doc_activity', artifact_id: this.artifactId, thread_id: row.authorThreadId, agent: row.author, kind: 'suggestion',
				span: row.from !== undefined && row.to !== undefined ? { from: row.from, to: row.to } : null,
				comment_id: row.id, revision: null, at: row.createdAt, call_id: null,
			});
		}
	}

	/** Release every hold on this document and stop waiting for activity frames. */
	releaseAll(): void {
		this.expected.forEach((timer) => clearTimeout(timer));
		this.expected.clear();
		releaseCommentArrivals(this.artifactId);
	}

	private holdForLane(commentId: string, callId: string | null): void {
		this.staged.add(commentId);
		const timer = this.expected.get(commentId);
		if (timer !== undefined) {
			// Already held from its change frame: the lane takes that hold over.
			clearTimeout(timer);
			this.expected.delete(commentId);
		} else if (!this.missed.delete(commentId) && !this.reducedMotion()) {
			holdCommentArrival(this.artifactId, commentId);
		}
		if (callId) holdCommentCall(this.artifactId, commentId, callId);
	}

	enqueue(frame: DocActivityFrame): void {
		// A comment arrives once: a second frame for one a lane already took (an unseen arrival, then its real frame) must not type it again.
		if ((frame.kind === 'comment' || frame.kind === 'suggestion') && frame.comment_id && this.staged.has(frame.comment_id)) return;
		let queue = this.queues.get(frame.thread_id);
		if (!queue) {
			queue = this.createQueue(frame.thread_id);
			this.queues.set(frame.thread_id, queue);
		}
		// The comment stays out of the document's comments from here until its turn (every exit releases).
		if ((frame.kind === 'comment' || frame.kind === 'suggestion') && frame.comment_id) this.holdForLane(frame.comment_id, frame.call_id ?? null);
		queue.enqueue({ frame });
		void queue.run().catch((error) => console.error('document arrival failed', error));
	}

	revealNow(threadId: string): void {
		this.queues.get(threadId)?.revealNow();
	}

	async wake(): Promise<void> {
		// Nobody is viewing this document: nothing stages its comments, so they show normally.
		if (!this.activeSurface()) this.releaseAll();
		this.reloadWhenTextCurrent();
		await Promise.all([...this.queues.values()].map((queue) => queue.wake()));
	}

	private activeSurface(): DocPresenceSurface | null {
		return [...this.surfaces.values()].find((surface) => surface.isViewing()) ?? null;
	}

	private reducedMotion(): boolean {
		return this.activeSurface()?.reducedMotion?.() ?? window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
	}

	private createQueue(threadId: string): ArrivalQueue<RuntimeArrival> {
		return createArrivalLane({
			artifactId: this.artifactId,
			activeSurface: () => this.activeSurface(),
			reducedMotion: () => this.reducedMotion(),
		}, threadId);
	}
}

const coordinators = new Map<string, ArtifactArrivalCoordinator>();
let startCount = 0;
let stopWebsocket: (() => void) | null = null;
let stopActivity: (() => void) | null = null;
let stopCommentChanges: (() => void) | null = null;
let stopReloads: (() => void) | null = null;
let stopSocket: (() => void) | null = null;
let expiryTimer: ReturnType<typeof setInterval> | null = null;

function coordinator(artifactId: string): ArtifactArrivalCoordinator {
	let current = coordinators.get(artifactId);
	if (!current) {
		current = new ArtifactArrivalCoordinator(artifactId);
		coordinators.set(artifactId, current);
	}
	return current;
}

function socketChanged(state: ConnectionState): void {
	if (state !== 'connected') {
		if (socket === 'up') socket = 'down';
		return;
	}
	const back = socket === 'down';
	socket = 'up';
	if (!back) return;
	reconnects += 1;
	for (const current of coordinators.values()) current.reloadAfterDrop();
}

function queueRecordedFrame(frame: DocActivityFrame): void {
	for (const pending of takePendingArrivals(frame.artifact_id, frame.thread_id)) {
		coordinator(frame.artifact_id).enqueue(pending);
	}
}

/** A change frame that names a new agent comment holds it at once, in the same dispatch as any reload listener. */
function holdNamedComment(frame: unknown): void {
	const { artifact_id: artifactId, comment_id: commentId, author_thread_id: authorThreadId } =
		(frame ?? {}) as Record<string, unknown>;
	if (typeof artifactId === 'string' && typeof commentId === 'string' && commentId && typeof authorThreadId === 'string' && authorThreadId) {
		coordinators.get(artifactId)?.expect(commentId);
	}
}

export function startDocPresence(): () => void {
	startCount += 1;
	if (startCount === 1) {
		stopActivity = onRecordedDocActivity(queueRecordedFrame);
		stopWebsocket = on('doc_activity', recordDocActivity);
		stopCommentChanges = on('doc_comment_changed', holdNamedComment);
		stopReloads = onCommentsReloaded((artifactId, unseen) => coordinators.get(artifactId)?.reloaded(unseen));
		stopSocket = connectionState.subscribe(socketChanged);
		expiryTimer = setInterval(() => prunePresence(), 30_000);
	}
	let stopped = false;
	return () => {
		if (stopped) return;
		stopped = true;
		startCount = Math.max(0, startCount - 1);
		if (startCount !== 0) return;
		stopWebsocket?.();
		stopActivity?.();
		stopCommentChanges?.();
		stopReloads?.();
		stopSocket?.();
		stopReloads = null;
		stopSocket = null;
		socket = 'unknown';
		stopWebsocket = null;
		stopActivity = null;
		stopCommentChanges = null;
		for (const current of coordinators.values()) current.releaseAll();
		if (expiryTimer) clearInterval(expiryTimer);
		expiryTimer = null;
		coordinators.clear();
	};
}

export function attachDocPresenceSurface(
	artifactId: string,
	surfaceId: string,
	surface: DocPresenceSurface,
): () => void {
	return coordinator(artifactId).attach(surfaceId, surface);
}

/** Show the comment an agent has pending now, as when the user clicks its anchor. */
export function revealPendingArrival(artifactId: string, threadId: string): void {
	coordinators.get(artifactId)?.revealNow(threadId);
}

export function notifyDocPresenceViewingChanged(artifactId: string | null): void {
	if (artifactId) void coordinator(artifactId).wake();
}
