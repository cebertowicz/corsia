import {
	answerReview, fetchReviewBar, ReviewRefusal,
	type FeedbackCounts, type ReviewAnswer, type ReviewBarRead, type ReviewVerdict,
} from '$lib/api/reviewBar';
import type { DocumentAsk } from '$lib/stores/documentBar';
import { personStepAsk } from '$lib/components/comments/commentTask';
import { createReviewBarRetry } from '$lib/reviewBarRetry';

export type ReviewBarPhase = 'ask' | 'noting' | 'sending' | 'answered' | 'stale';
export type ReviewAnswerOutcome = 'sent' | 'ignored' | 'failed';
export interface ReviewReceipt { verdict: ReviewVerdict; sent: FeedbackCounts; at: Date }

export const SEND_FAILED = "Couldn't send. Try again.";
export const NOTE_NEEDED = 'Say what should change.';
export const DRAFT_OPEN = 'Finish or discard your comment first';

/**
 * One artifact's review ask. Every asynchronous result belongs to the artifact generation that
 * started it, and the receipt is client state: it is shown only once the server has confirmed the
 * answer, and it stays until the artifact changes or a different ask is raised.
 */
export function createReviewBarState() {
	let artifactId: string | null = null;
	let generation = 0;
	let loadSequence = 0;
	const retry = createReviewBarRetry(() => void load(true));
	function deactivate() { loadSequence += 1; retry.reset(); }
	/** The raise time of the ask the receipt or the replaced notice belongs to. */
	let settledAsk: string | null = null;
	let read = $state<ReviewBarRead | null>(null);
	let phase = $state<ReviewBarPhase>('ask');
	/** The layout a send came from: the sending bar keeps it, so nothing remounts under the user's hands. */
	let sendingFrom = $state<'ask' | 'noting'>('ask');
	let note = $state('');
	let accepting = $state(false);
	let failure = $state('');
	/** The server refused a verdict without a note: the note stays required for this ask whatever the local count says. */
	let noteDemanded = $state(false);
	let receipt = $state<ReviewReceipt | null>(null);
	const current = (id: string, stamp: number) => artifactId === id && generation === stamp;

	const noteRequired = () => noteDemanded || (read?.round_feedback_count ?? 0) === 0;
	const visible = () => phase === 'answered' || phase === 'stale' || phase === 'sending' || Boolean(read?.ask_raised);

	function setTarget(id: string | null): boolean {
		if (id === artifactId) return false;
		retry.reset();
		artifactId = id; generation += 1; loadSequence += 1; settledAsk = null;
		read = null; phase = 'ask'; note = ''; failure = ''; receipt = null; noteDemanded = false;
		return true;
	}
	async function load(isRetry = false) {
		if (isRetry) retry.cancel(); else retry.reset();
		const id = artifactId, stamp = generation, sequence = ++loadSequence;
		if (!id) return;
		try {
			const next = await fetchReviewBar(id);
			if (!current(id, stamp) || sequence !== loadSequence) return;
			retry.reset();
			read = next;
			if ((phase === 'answered' || phase === 'stale') && next.ask_raised && next.ask_raised_at !== settledAsk) {
				phase = 'ask'; receipt = null; failure = ''; note = ''; settledAsk = null; noteDemanded = false;
			} else if (phase === 'noting' && !next.ask_raised) {
				phase = 'ask'; note = '';
			}
		} catch {
			if (!current(id, stamp) || sequence !== loadSequence) return;
			retry.schedule();
		}
	}
	function openNote() { if (phase === 'ask') { failure = ''; phase = 'noting'; } }
	function cancelNote() { if (phase === 'noting') { note = ''; failure = ''; phase = 'ask'; } }

	/** Send the verdict; `from` is where a failed send returns to, with the note kept. */
	async function answer(
		verdict: 'approved' | 'changes_requested',
		onSendStarted?: () => void,
		onSendConfirmed?: (answer: ReviewAnswer) => void,
	): Promise<ReviewAnswerOutcome> {
		const id = artifactId, stamp = generation;
		if (!id || (phase !== 'ask' && phase !== 'noting')) return 'ignored';
		if (verdict !== 'approved' && noteRequired() && !note.trim()) { failure = NOTE_NEEDED; return 'ignored'; }
		const from = phase;
		sendingFrom = from;
		phase = 'sending'; failure = '';
		onSendStarted?.();
		try {
			const sent = await answerReview(id, verdict, verdict !== 'approved' ? note : '', read?.hold_ids ?? []);
			// The hold-keyed card overlay must receive the canonical server response even if this bar moved on.
			onSendConfirmed?.(sent);
			if (!current(id, stamp)) return 'sent';
			const counts = sent.feedback_counts;
			settledAsk = read?.ask_raised_at ?? null;
			receipt = { verdict: sent.verdict, sent: counts, at: new Date() };
			phase = 'answered';
			return 'sent';
		} catch (error) {
			if (!current(id, stamp)) return 'failed';
			if (error instanceof ReviewRefusal && error.code === 'ask_stale') {
				settledAsk = read?.ask_raised_at ?? null;
				phase = 'stale';
				await load();
			} else if (error instanceof ReviewRefusal && error.code === 'note_required') {
				noteDemanded = true; phase = 'noting'; failure = NOTE_NEEDED;
			} else {
				phase = from; failure = SEND_FAILED;
			}
			return 'failed';
		}
	}
	return {
		get read() { return read; }, get phase() { return phase; }, get sendingFrom() { return sendingFrom; }, get receipt() { return receipt; },
		get note() { return note; }, set note(value: string) { note = value; },
		get accepting() { return accepting; }, set accepting(value: boolean) { accepting = value; },
		get failure() { return failure; }, set failure(value: string) { failure = value; },
		get visible() { return visible(); },
		get noteRequired() { return noteRequired(); },
		get artifactId() { return artifactId; },
		setTarget, load, deactivate, openNote, cancelNote, answer,
	};
}
export type ReviewBarState = ReturnType<typeof createReviewBarState>;

/** The ask waiting on a document, for its Details while the card is hidden; `hidden` is the card stepped aside under a comment sheet. */
export function documentAskOf(bar: Pick<ReviewBarState, 'read' | 'phase'>, hidden: boolean): DocumentAsk | null {
	const read = bar.read;
	if (!read || bar.phase === 'answered' || bar.phase === 'stale') return null;
	if (read.person_step) return { who: read.person_step.owner_name, text: personStepAsk(read.person_step), others: [], onCard: !hidden };
	if (!read.ask_raised) return null;
	return { who: read.asker?.name ?? 'An agent', text: read.ask_reason ?? '', others: read.other_asks ?? [], onCard: !hidden };
}
