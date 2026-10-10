import type { Message } from '$lib/stores/threads';
import type { Attachment } from './inputBarHelpers';

export type ComposerDraft = {
	content: string;
	attachments: Attachment[];
};

type PendingSubmitDraft = {
	composerDraft: ComposerDraft;
	threadId: string;
};

/** The composer state a draft controller reads and writes, owned by InputBar. */
export interface ComposerPort {
	/** The composer's current text and attachments, or null when it is empty. */
	current(): ComposerDraft | null;
	/** Put a draft (or nothing) back into the composer. */
	restore(draft: ComposerDraft | undefined): void;
	/** Clear the composer after a send. */
	reset(): void;
	/** The thread this composer sends to. */
	boundThreadId(): string | null;
	/** Whether the composer holds any text right now, trimmed or not. */
	hasText(): boolean;
}

export function cloneDraft(draft: ComposerDraft): ComposerDraft {
	return {
		content: draft.content,
		attachments: [...draft.attachments],
	};
}

function sameAttachments(left: Attachment[], right: Attachment[]): boolean {
	return left.length === right.length && left.every((attachment, index) =>
		attachment.file_id === right[index]?.file_id
	);
}

function sameComposerDraft(left: ComposerDraft, right: ComposerDraft): boolean {
	return left.content === right.content
		&& sameAttachments(left.attachments, right.attachments);
}

/**
 * Per-thread composer drafts plus the recovery copy of each submitted draft.
 *
 * Recovery state only: the durable FIFO owns ordering, persistence, and
 * delivery; a submitted draft is kept until its receipt so a socket failure
 * cannot erase the user's text.
 */
export function createComposerDrafts(port: ComposerPort) {
	const threadDrafts = new Map<string, ComposerDraft>();
	const pendingSubmitDrafts = new Map<string, PendingSubmitDraft>();
	let lastThreadId: string | null = null;

	function matchesComposerDraft(draft: ComposerDraft): boolean {
		const current = port.current();
		return current !== null && sameComposerDraft(current, draft);
	}

	function saveCurrentDraft(threadId: string): void {
		const draft = port.current();
		if (draft) threadDrafts.set(threadId, cloneDraft(draft));
		else threadDrafts.delete(threadId);
	}

	/** Save the draft of the thread being left and restore the one being entered. */
	function switchThread(sid: string | null): void {
		if (lastThreadId && lastThreadId !== sid) saveCurrentDraft(lastThreadId);
		if (sid && sid !== lastThreadId) {
			const transitioningFromEmpty = lastThreadId === null && port.hasText();
			if (!transitioningFromEmpty) {
				port.restore(threadDrafts.get(sid));
			}
		}
		lastThreadId = sid;
	}

	function restoreSubmittedDraft(pending: PendingSubmitDraft): void {
		const { composerDraft, threadId: draftThreadId } = pending;
		if (port.boundThreadId() !== draftThreadId) {
			if (!threadDrafts.has(draftThreadId)) {
				threadDrafts.set(draftThreadId, cloneDraft(composerDraft));
			}
			return;
		}
		if (!port.current()) {
			port.restore(composerDraft);
		}
	}

	function settleDeliveredDraft(pending: PendingSubmitDraft): void {
		const { composerDraft, threadId: draftThreadId } = pending;
		const saved = threadDrafts.get(draftThreadId);
		if (saved && saved.content === composerDraft.content
			&& sameAttachments(saved.attachments, composerDraft.attachments)) {
			threadDrafts.delete(draftThreadId);
		}
		if (port.boundThreadId() === draftThreadId && matchesComposerDraft(composerDraft)) {
			port.reset();
		}
	}

	/** Settle a submitted draft from a delivery event: clear it on receipt, restore it on failure. */
	function settlePendingSubmit(
		data: any,
		fallbackState?: Message['delivery_state'],
	): void {
		const inputId = typeof data?.message?.metadata?.input_id === 'string'
			? data.message.metadata.input_id
			: typeof data?.input_id === 'string'
				? data.input_id
				: null;
		if (!inputId) return;
		const pending = pendingSubmitDrafts.get(inputId);
		if (!pending || data?.thread_id !== pending.threadId) return;
		const state = data?.message?.delivery_state ?? data?.delivery_state ?? fallbackState;
		if (state === 'queued' || state === 'delivered') {
			pendingSubmitDrafts.delete(inputId);
			settleDeliveredDraft(pending);
			return;
		}
		if (state === 'ambiguous' || state === 'cancelled' || state === 'failed') {
			pendingSubmitDrafts.delete(inputId);
			restoreSubmittedDraft(pending);
		}
	}

	/** Remember a just-sent draft until its delivery settles. */
	function trackSubmit(inputId: string, threadId: string, draft: ComposerDraft): void {
		pendingSubmitDrafts.set(inputId, {
			composerDraft: cloneDraft(draft),
			threadId,
		});
	}

	return { threadDrafts, switchThread, saveCurrentDraft, settlePendingSubmit, trackSubmit };
}
