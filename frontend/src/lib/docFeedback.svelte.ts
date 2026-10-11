import { onDestroy, untrack } from 'svelte';
import {
	leaveDocFeedback,
	retryDocFeedback,
	returnDocFeedback,
	sendDocFeedback,
	type DocFeedbackResult,
	type UndeliveredBatch,
} from '$lib/api/docFeedback';
import type { CommentFeedback } from '$lib/api/docs';
import { toasts } from '$lib/stores/toast';

const DELIVERY_REASONS: Record<string, string> = {
	no_owner: 'this document has no owner',
	owner_archived: 'its owner is archived',
	owner_unavailable: 'its owner is unavailable',
	owner_admission_refused: "its owner couldn't take it right now",
	owner_unaddressable: "its owner can't be reached",
};

/** How long Send shows "Sent" after the comments went, before it goes. */
export const SENT_HOLD_MS = 1500;

/**
 * One open document's feedback batch as the user sees it: the comments waiting to go to its owner, the Send that
 * closes the batch, and what could not be delivered. The open tab holds it (`holdDocFeedback`); an image viewer over
 * a document shows and sends the document's batch (DCOL-FR13.12).
 */
export class DocFeedback {
	artifactId = $state<string | null>(null);
	targetInstanceId = $state<string | null>(null);
	pendingCount = $state(0);
	undelivered = $state<UndeliveredBatch[]>([]);
	sending = $state(false);
	retrying = $state(false);
	leaveSent = $state(false);
	feedback = $state<CommentFeedback | null>(null);
	countdownNow = $state(Date.now());
	/** How many comments the last Send delivered, while it says so; 0 otherwise. */
	sent = $state(0);
	/** The last Send ended in a batch not delivered: Retry takes Send's place at once, so Send does not close beside it. */
	handedToRetry = false;
	private sentTimer: ReturnType<typeof setTimeout> | undefined;

	readonly undeliveredCount = $derived(this.undelivered.reduce((count, batch) => count + batch.item_count, 0));
	readonly undeliveredReason = $derived.by(() => {
		const reasons = new Set(this.undelivered.map((batch) => batch.undelivered_reason));
		if (reasons.size !== 1) return '';
		const [reason] = reasons;
		return DELIVERY_REASONS[reason] ?? "it couldn't be delivered";
	});
	readonly undeliveredSummary = $derived(
		`${this.undeliveredCount} comment${this.undeliveredCount === 1 ? '' : 's'} not delivered${this.undeliveredReason ? `: ${this.undeliveredReason}` : ''}`,
	);
	readonly ownerName = $derived(this.feedback?.owner_label ?? null);
	readonly sendLabel = $derived(
		`Send ${this.pendingCount} comment${this.pendingCount === 1 ? '' : 's'}${this.ownerName ? ` to ${this.ownerName}` : ''}`,
	);
	readonly graceDeadline = $derived(this.feedback?.grace_closes_at ?? null);
	readonly graceMinutes = $derived.by(() => {
		if (!this.graceDeadline) return null;
		return Math.max(0, Math.ceil((Date.parse(this.graceDeadline) - this.countdownNow) / 60_000));
	});
	readonly sendingGrace = $derived(this.pendingCount > 0 && this.graceMinutes !== null);
	readonly graceText = $derived(`Sending to ${this.ownerName ?? 'the owner'} in ${this.graceMinutes} min`);
	readonly sentText = $derived(`${this.sent} comment${this.sent === 1 ? '' : 's'} sent${this.ownerName ? ` to ${this.ownerName}` : ''}`);

	/** Another document (or tab) is shown: leave the one before, and start afresh. */
	show(artifactId: string | null, targetInstanceId: string | null) {
		if (this.artifactId === artifactId && this.targetInstanceId === targetInstanceId) return;
		this.leave();
		this.confirmSent(0);
		this.artifactId = artifactId;
		this.targetInstanceId = targetInstanceId;
		this.pendingCount = 0;
		this.undelivered = [];
		this.leaveSent = false;
		this.feedback = null;
	}

	commentCreated() {
		this.confirmSent(0);
		this.pendingCount += 1;
		this.leaveSent = false;
	}

	sync(id: string, current: CommentFeedback) {
		if (this.artifactId !== id) return;
		this.feedback = current;
		this.pendingCount = current.open_count;
		this.undelivered = current.undelivered_batches;
		if (this.pendingCount > 0) {
			this.leaveSent = false;
			this.confirmSent(0);
		}
		// Only a visible document counts as the user returning; a refresh in a hidden tab must not.
		if (document.visibilityState === 'visible' && !current.grace_closes_at) {
			void returnDocFeedback(id).catch((error) => console.error('feedback return failed', error));
		}
	}

	private applyResult(id: string, result: DocFeedbackResult) {
		if (this.artifactId === id) this.undelivered = result.undelivered_batches;
	}

	/** Say `count` comments just went, for SENT_HOLD_MS; 0 stops saying it. */
	private confirmSent(count: number) {
		clearTimeout(this.sentTimer);
		this.sent = count;
		if (count > 0) this.sentTimer = setTimeout(() => { this.sent = 0; }, SENT_HOLD_MS);
	}

	send = async () => {
		const id = this.artifactId;
		if (!id || this.pendingCount === 0) return;
		const count = this.pendingCount;
		const undeliveredBefore = this.undeliveredCount;
		this.sending = true;
		this.handedToRetry = false;
		try {
			this.applyResult(id, await sendDocFeedback(id));
			if (this.artifactId === id) {
				this.handedToRetry = this.undeliveredCount > undeliveredBefore;
				this.pendingCount = 0;
				// A batch that could not be delivered says so in its own line; only one that went says Sent.
				if (!this.handedToRetry) this.confirmSent(count);
			}
		} catch {
			toasts.show("Couldn't send your comments — try again", 'error');
		} finally {
			this.sending = false;
		}
	};

	retry = async () => {
		const id = this.artifactId;
		if (!id) return;
		this.retrying = true;
		try {
			this.applyResult(id, await retryDocFeedback(id));
		} catch {
			toasts.show("Couldn't send your comments — try again", 'error');
		} finally {
			this.retrying = false;
		}
	};

	leave = () => {
		const id = this.artifactId;
		if (!id || this.pendingCount === 0 || this.leaveSent) return;
		this.leaveSent = true;
		this.pendingCount = 0;
		void leaveDocFeedback(id).then((result) => this.applyResult(id, result)).catch((error) => {
			this.leaveSent = false;
			console.error('feedback leave failed', error);
		});
	};
}

/**
 * The batch for the target `of()` names, held for as long as the calling component (the open tab) is mounted, so
 * reloading what the tab shows is not leaving it. Leaving is another target, a hidden browser tab, closing the page,
 * or the component going. `onReturn` runs when the browser tab shows again. Call while the component initialises.
 */
export function holdDocFeedback(
	of: () => { artifactId: string | null; targetInstanceId: string | null },
	onReturn?: () => void,
): DocFeedback {
	const feedback = new DocFeedback();
	$effect(() => {
		const { artifactId, targetInstanceId } = of();
		untrack(() => feedback.show(artifactId, targetInstanceId));
	});
	$effect(() => {
		if (!feedback.graceDeadline) return;
		const timer = setInterval(() => { feedback.countdownNow = Date.now(); }, 30_000);
		return () => clearInterval(timer);
	});
	$effect(() => {
		const visibility = () => {
			if (document.visibilityState === 'hidden') feedback.leave();
			if (document.visibilityState === 'visible') onReturn?.();
		};
		window.addEventListener('visibilitychange', visibility);
		window.addEventListener('beforeunload', feedback.leave);
		return () => {
			window.removeEventListener('visibilitychange', visibility);
			window.removeEventListener('beforeunload', feedback.leave);
		};
	});
	onDestroy(feedback.leave);
	return feedback;
}
