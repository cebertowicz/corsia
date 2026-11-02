import {
	ArrivalQueue,
	onRecordedDocActivity,
	prunePresence,
	recordDocActivity,
	setPresenceHighlight,
	takePendingArrivals,
	type DocActivityFrame,
	type DocSpan,
} from '$lib/docPresence';
import { holdCommentArrival, holdCommentCall, releaseCommentArrival, releaseCommentArrivals } from '$lib/stores/docCommentArrivals';
import { onUnseenSuggestions, type DocComment } from '$lib/stores/docComments';
import { on } from '$lib/ws/events';
import type { SelectOutcome } from '$lib/cm/typed-reveal-arrival';

export interface PreparedPresenceArrival {
	chars: number;
	/** Display time before the reveal when no editor runs the select (sweep plus hold); defaults to a delay scaled by `chars`. */
	delayMs?: number;
	payload?: unknown;
	highlightSpan?: DocSpan | null;
}

export interface DocPresenceSurface {
	isViewing: () => boolean;
	prepare: (frame: DocActivityFrame) => Promise<PreparedPresenceArrival>;
	reveal: (frame: DocActivityFrame, payload: unknown) => void | Promise<void>;
	/**
	 * Run the arrival's select step in this surface's editor and report how it ended, or null when no
	 * editor can run one. When it runs it is the arrival's only highlight and clock.
	 */
	select?: (frame: DocActivityFrame, prepared: PreparedPresenceArrival) => Promise<SelectOutcome> | null;
	reducedMotion?: () => boolean;
}

interface RuntimeArrival {
	frame: DocActivityFrame;
	prepared?: PreparedPresenceArrival;
	failed?: boolean;
}

/** How long a comment named by a change frame is held while its activity frame is awaited. */
const MISSING_FRAME_WAIT_MS = 5_000;

class ArtifactArrivalCoordinator {
	private readonly surfaces = new Map<string, DocPresenceSurface>();
	private readonly queues = new Map<string, ArrivalQueue<RuntimeArrival>>();
	/** Comments held on their change frame, each with the timer that ends the wait for its activity frame. */
	private readonly expected = new Map<string, ReturnType<typeof setTimeout>>();
	/** Comments released because no activity frame came; a late frame must not pull them back out. */
	private readonly missed = new Set<string>();
	/** Comments a lane has taken, kept after they show: a comment arrives once, so a later change or activity frame leaves it alone. */
	private readonly staged = new Set<string>();

	constructor(private readonly artifactId: string) {}

	attach(surfaceId: string, surface: DocPresenceSurface): () => void {
		this.surfaces.set(surfaceId, surface);
		void this.wake();
		return () => {
			this.surfaces.delete(surfaceId);
			if (!this.surfaces.size) this.releaseAll();
		};
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

	/** A reload found suggestions this canvas never saw (their frames were missed): arrive them as their activity frame would have. */
	arriveUnseen(rows: DocComment[]): void {
		if (!this.activeSurface() || this.reducedMotion()) return;
		for (const row of rows) {
			recordDocActivity({
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
		// A second frame for a comment a lane already took (an unseen arrival, then its real frame) must not type it twice.
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
		await Promise.all([...this.queues.values()].map((queue) => queue.wake()));
	}

	private activeSurface(): DocPresenceSurface | null {
		return [...this.surfaces.values()].find((surface) => surface.isViewing()) ?? null;
	}

	private reducedMotion(): boolean {
		return this.activeSurface()?.reducedMotion?.() ?? window.matchMedia?.('(prefers-reduced-motion: reduce)').matches ?? false;
	}

	private release(arrival: RuntimeArrival): void {
		if ((arrival.frame.kind !== 'comment' && arrival.frame.kind !== 'suggestion') || !arrival.frame.comment_id) return;
		releaseCommentArrival(this.artifactId, arrival.frame.comment_id);
	}

	private createQueue(threadId: string): ArrivalQueue<RuntimeArrival> {
		return new ArrivalQueue<RuntimeArrival>({
			isViewing: () => this.activeSurface() !== null,
			reducedMotion: () => this.reducedMotion(),
			paused: (waiting) => waiting.forEach((arrival) => this.release(arrival)),
			prepare: async (arrival) => {
				const surface = this.activeSurface();
				if (!surface) return { value: arrival, chars: 0 };
				try {
					arrival.prepared = await surface.prepare(arrival.frame);
					return { value: arrival, chars: arrival.prepared.chars, delayMs: arrival.prepared.delayMs };
				} catch (error) {
					arrival.failed = true;
					console.error('document arrival preparation failed', error);
					return { value: arrival, chars: 0 };
				}
			},
			select: (arrival) => {
				if (arrival.failed || !arrival.prepared) return null;
				return this.activeSurface()?.select?.(arrival.frame, arrival.prepared) ?? null;
			},
			highlight: (arrival, selecting) => {
				if (arrival.failed) return;
				// The editor draws a select it runs itself; the presence mark would be a second highlight.
				if (selecting) { setPresenceHighlight(this.artifactId, threadId, null, true); return; }
				const span = arrival.prepared && 'highlightSpan' in arrival.prepared
					? arrival.prepared.highlightSpan ?? null
					: arrival.frame.span;
				setPresenceHighlight(this.artifactId, threadId, span, true, arrival.frame.kind);
			},
			reveal: async (arrival) => {
				try {
					if (arrival.failed) return;
					const surface = this.activeSurface();
					if (surface) await surface.reveal(arrival.frame, arrival.prepared?.payload);
				} finally {
					this.release(arrival);
				}
			},
			clear: (arrival) => {
				setPresenceHighlight(this.artifactId, threadId, null, false);
				this.release(arrival);
			},
		});
	}
}

const coordinators = new Map<string, ArtifactArrivalCoordinator>();
let startCount = 0;
let stopWebsocket: (() => void) | null = null;
let stopActivity: (() => void) | null = null;
let stopCommentChanges: (() => void) | null = null;
let stopUnseen: (() => void) | null = null;
let expiryTimer: ReturnType<typeof setInterval> | null = null;

function coordinator(artifactId: string): ArtifactArrivalCoordinator {
	let current = coordinators.get(artifactId);
	if (!current) {
		current = new ArtifactArrivalCoordinator(artifactId);
		coordinators.set(artifactId, current);
	}
	return current;
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
		stopUnseen = onUnseenSuggestions((artifactId, rows) => coordinators.get(artifactId)?.arriveUnseen(rows));
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
		stopUnseen?.();
		stopUnseen = null;
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
