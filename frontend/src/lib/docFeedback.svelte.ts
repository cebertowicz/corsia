import {
	retryDocFeedback,
	sendDocFeedback,
	type DeliveryPolicy,
	type DocFeedbackResult,
	type UndeliveredBatch,
} from '$lib/api/docFeedback';
import type { CommentFeedback } from '$lib/api/docs';
import { toasts } from '$lib/stores/toast';
import { deliveryConfirmation } from '$lib/stores/deliveryConfirmation';

const DELIVERY_REASONS: Record<string, string> = {
	no_owner: 'this document has no owner',
	owner_archived: 'its owner is archived',
	owner_unavailable: 'its owner is unavailable',
	owner_admission_refused: "its owner couldn't take it right now",
	owner_unaddressable: "its owner can't be reached",
};

/** How long Send shows "Sent" after the comments went, before it goes. */
export const SENT_HOLD_MS = 1500;

/** The two held lanes a person can send: a document's comments, and the comments on its images. */
export type HeldLane = 'document-held' | 'image-held';

/** The batches each lane's control shows a failure for: the document header also carries a failed immediate delivery. */
const LANE_POLICIES: Record<HeldLane, readonly DeliveryPolicy[]> = {
	'document-held': ['document-held', 'immediate'],
	'image-held': ['image-held'],
};

const plural = (count: number) => `${count} comment${count === 1 ? '' : 's'}`;

/**
 * One open document's feedback batch as the user sees it: the comments waiting to go to its owner, the Send that
 * closes each held lane, and what could not be delivered. The open tab holds it (`holdDocFeedback`); an image viewer
 * over a document shows and sends the document's image lane (DCOL-FR13.12).
 */
export class DocFeedback {
	artifactId = $state<string | null>(null);
	targetInstanceId = $state<string | null>(null);
	/** Every unsent comment on the document, whichever lane holds it. */
	pendingCount = $state(0);
	documentHeld = $state(0);
	imageHeld = $state(0);
	undelivered = $state<UndeliveredBatch[]>([]);
	/** The lane whose Send or Retry is in flight for this document, if any. */
	sendingLane = $state<HeldLane | null>(null);
	retryingLane = $state<HeldLane | null>(null);
	feedback = $state<CommentFeedback | null>(null);
	/** How many comments the last Send delivered, while it says so; 0 otherwise. */
	sent = $state(0);
	sentLane = $state<HeldLane | null>(null);
	/** The last Send ended in a batch not delivered: Retry takes Send's place at once, so Send does not close beside it. */
	handedToRetry = false;
	private sentTimer: ReturnType<typeof setTimeout> | undefined;
	private lastDocumentTouchAt = 0;
	private lastImageTouchAt = 0;
	/** A comment was made since the counts were last read, so the image lane may hold one the counts do not show yet. */
	private unsynced = false;
	private flights = new Map<string, Promise<void>>();
	private revision = 0;

	constructor(private readonly refresh?: () => void | Promise<void>) {}

	readonly sending = $derived(this.sendingLane !== null);

	undeliveredFor(lane: HeldLane | null): UndeliveredBatch[] {
		return lane === null ? [] : this.undelivered.filter((batch) => LANE_POLICIES[lane].includes(batch.delivery_policy));
	}

	undeliveredCount(lane: HeldLane | null): number {
		return this.undeliveredFor(lane).reduce((count, batch) => count + batch.item_count, 0);
	}

	undeliveredSummary(lane: HeldLane | null): string {
		const batches = this.undeliveredFor(lane);
		const count = batches.reduce((total, batch) => total + batch.item_count, 0);
		const reasons = new Set(batches.map((batch) => batch.undelivered_reason));
		const reason = reasons.size === 1 ? DELIVERY_REASONS[[...reasons][0]] ?? "it couldn't be delivered" : '';
		return `${count} comment${count === 1 ? '' : 's'} not delivered${reason ? `: ${reason}` : ''}`;
	}
	readonly ownerName = $derived(this.feedback?.owner_label ?? null);
	readonly sentText = $derived(`${plural(this.sent)} sent${this.ownerName ? ` to ${this.ownerName}` : ''}`);

	heldCount(lane: HeldLane): number {
		return lane === 'document-held' ? this.documentHeld : this.imageHeld;
	}
	/** The Send's visible words: the count alone, so 101 stays on one line. */
	sendText(lane: HeldLane): string {
		return `Send ${plural(this.heldCount(lane))}`;
	}
	/** The Send's spoken name, which also says who gets the comments. */
	sendLabel(lane: HeldLane): string {
		return `${this.sendText(lane)}${this.ownerName ? ` to ${this.ownerName}` : ''}`;
	}

	/** Another document (or tab) is shown: start afresh. */
	show(artifactId: string | null, targetInstanceId: string | null) {
		if (this.artifactId === artifactId && this.targetInstanceId === targetInstanceId) return;
		this.revision += 1;
		this.confirmSent(0);
		this.artifactId = artifactId;
		this.targetInstanceId = targetInstanceId;
		this.pendingCount = 0;
		this.documentHeld = 0;
		this.imageHeld = 0;
		this.unsynced = false;
		this.undelivered = [];
		this.feedback = null;
	}

	/** A person's comment is delivered as it is made, so it leaves nothing unsent; only a stale "sent" line goes. */
	commentCreated() {
		this.revision += 1;
		this.confirmSent(0);
		this.unsynced = true;
		window.dispatchEvent(new Event('doc-feedback-changed'));
	}

	sync(id: string, current: CommentFeedback) {
		if (this.artifactId !== id) return;
		this.revision += 1;
		this.feedback = current;
		this.applyCounts({
			open_count: current.open_count,
			document_held_count: current.document_held_count ?? 0,
			image_held_count: current.image_held_count ?? 0,
		});
		this.undelivered = current.undelivered_batches;
		this.unsynced = false;
		if (this.pendingCount > 0) {
			this.confirmSent(0);
		}
	}

	private applyCounts(counts: { open_count: number; document_held_count: number; image_held_count: number }) {
		this.pendingCount = counts.open_count;
		this.documentHeld = counts.document_held_count;
		this.imageHeld = counts.image_held_count;
	}

	/** What a send reports is the truth: the lane it closed is empty, and every other lane keeps the server's count. */
	private applyResult(id: string, lane: HeldLane, result: DocFeedbackResult, revision: number) {
		// A matching sync or new comment since the request began is newer than this send's snapshot.
		if (this.artifactId === id && this.revision === revision) {
			this.undelivered = result.undelivered_batches;
			this.applyCounts(result);
			this.unsynced = false;
		}
		window.dispatchEvent(new Event('doc-feedback-changed'));
	}

	/**
	 * Say what one call delivered, from its own result alone: the server counts only comments whose batches ended
	 * delivered, so a send or retry that ended undelivered, or that another sender won, confirms nothing. Every held-lane
	 * trigger (button, image exit, pagehide, thread change, retry) reaches this one canvas-local confirmation.
	 */
	private reportDelivered(id: string, lane: HeldLane, result: DocFeedbackResult) {
		const sent = result.sent_count ?? 0;
		if (sent === 0) return;
		if (this.artifactId === id) this.confirmSent(sent, lane);
		deliveryConfirmation.show(id, sent);
	}

	/** Say `count` comments just went, for SENT_HOLD_MS; 0 stops saying it. */
	private confirmSent(count: number, lane: HeldLane | null = null) {
		clearTimeout(this.sentTimer);
		this.sent = count;
		this.sentLane = count > 0 ? lane : null;
		if (count > 0) this.sentTimer = setTimeout(() => { this.sent = 0; this.sentLane = null; }, SENT_HOLD_MS);
	}

	/**
	 * Send one lane of a document. Overlapping callers (a button and an exit) share one request, and the server closes a
	 * batch only while it is open, so a sender that lands second delivers nothing and just reports what remains.
	 */
	sendLane(lane: HeldLane, id: string | null = this.artifactId, keepalive = false): Promise<void> {
		if (!id) return Promise.resolve();
		const key = `${id}\u0000${lane}`;
		const running = this.flights.get(key);
		if (running) return running;
		const flight = this.deliver(id, lane, keepalive).finally(() => this.flights.delete(key));
		this.flights.set(key, flight);
		return flight;
	}

	sendDocument = () => this.sendLane('document-held');
	sendImage = () => this.sendLane('image-held');

	/**
	 * Leaving an image (Back, Escape, another tab, a collapsed canvas, another thread, closing the page) sends what
	 * its viewer still holds, once. Nothing is sent when the lane is known to be empty.
	 */
	leaveImage = (id: string | null = this.artifactId, keepalive = false): Promise<void> => {
		if (!id) return Promise.resolve();
		if (id === this.artifactId && this.imageHeld === 0 && !this.unsynced) return Promise.resolve();
		return this.sendLane('image-held', id, keepalive);
	};

	private async deliver(id: string, lane: HeldLane, keepalive: boolean) {
		const revision = this.revision;
		const shown = this.artifactId === id;
		if (shown) {
			this.sendingLane = lane;
			this.handedToRetry = false;
		}
		try {
			const result = await sendDocFeedback(id, lane, keepalive);
			this.applyResult(id, lane, result, revision);
			if (this.artifactId === id) this.handedToRetry = (result.undelivered_count ?? 0) > 0;
			this.reportDelivered(id, lane, result);
			// Keep the lane busy until its owning reader settles any overlapping mutation.
			if (this.artifactId === id) await this.refresh?.();
		} catch {
			toasts.show("Couldn't send your comments — try again", 'error');
		} finally {
			if (this.sendingLane === lane && shown) this.sendingLane = null;
		}
	}

	touchDocument = () => this.touch('document-held', 30_000, 'lastDocumentTouchAt');
	touchImage = () => this.touch('image-held', 60_000, 'lastImageTouchAt');

	private touch(
		policy: 'document-held' | 'image-held',
		interval: number,
		field: 'lastDocumentTouchAt' | 'lastImageTouchAt',
	) {
		const id = this.artifactId;
		const now = Date.now();
		if (!id || now - this[field] < interval) return;
		this[field] = now;
		void import('$lib/api/docFeedback')
			.then(({ touchDocFeedback }) => touchDocFeedback(id, policy))
			.catch((error) => console.warn('[doc feedback] activity touch failed', error));
	}

	retry = async (lane: HeldLane) => {
		const id = this.artifactId;
		if (!id || this.retryingLane !== null) return;
		this.retryingLane = lane;
		const revision = this.revision;
		try {
			const result = await retryDocFeedback(id, lane);
			this.applyResult(id, lane, result, revision);
			this.reportDelivered(id, lane, result);
		} catch {
			toasts.show("Couldn't send your comments — try again", 'error');
		} finally {
			if (this.retryingLane === lane) this.retryingLane = null;
		}
	};
}

// Lifecycle effects live in docFeedbackLifecycle.svelte.ts; this module owns only state and transitions.
