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

/**
 * One open document's feedback batch as the user sees it: the comments waiting to go to its owner, the Send that
 * closes the batch, and what could not be delivered. The document's status row owns it (and its leaving); an image
 * viewer over the document shows and sends the same batch (DCOL-FR13.12).
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

	/** Another document (or tab) is shown: leave the one before, and start afresh. */
	show(artifactId: string | null, targetInstanceId: string | null) {
		if (this.artifactId === artifactId && this.targetInstanceId === targetInstanceId) return;
		this.leave();
		this.artifactId = artifactId;
		this.targetInstanceId = targetInstanceId;
		this.pendingCount = 0;
		this.undelivered = [];
		this.leaveSent = false;
		this.feedback = null;
	}

	commentCreated() {
		this.pendingCount += 1;
		this.leaveSent = false;
	}

	sync(id: string, current: CommentFeedback) {
		if (this.artifactId !== id) return;
		this.feedback = current;
		this.pendingCount = current.open_count;
		this.undelivered = current.undelivered_batches;
		if (this.pendingCount > 0) this.leaveSent = false;
		// Only a visible document counts as the user returning; a refresh in a hidden tab must not.
		if (document.visibilityState === 'visible' && !current.grace_closes_at) {
			void returnDocFeedback(id).catch((error) => console.error('feedback return failed', error));
		}
	}

	private applyResult(id: string, result: DocFeedbackResult) {
		if (this.artifactId === id) this.undelivered = result.undelivered_batches;
	}

	send = async () => {
		const id = this.artifactId;
		if (!id || this.pendingCount === 0) return;
		this.sending = true;
		try {
			this.applyResult(id, await sendDocFeedback(id));
			if (this.artifactId === id) this.pendingCount = 0;
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
