import {
	answerHoldIds, answerReview, fetchReviewBar, ReviewRefusal,
	type FeedbackCounts, type ReviewAnswer, type ReviewBarRead, type ReviewVerdict,
} from '$lib/api/reviewBar';

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
	/** The raise time of the ask the receipt or the replaced notice belongs to. */
	let settledAsk: string | null = null;
	let read = $state<ReviewBarRead | null>(null);
	let phase = $state<ReviewBarPhase>('ask');
	/** The layout a send came from: the sending bar keeps it, so nothing remounts under the user's hands. */
	let sendingFrom = $state<'ask' | 'noting'>('ask');
	let note = $state('');
	let noteFor = $state<'changes_requested' | 'approved_with_changes'>('changes_requested');
	let accepting = $state(false);
	let failure = $state('');
	/** The server refused a verdict without a note: the note stays required for this ask whatever the local count says. */
	let noteDemanded = $state(false);
	let receipt = $state<ReviewReceipt | null>(null);
	/** An email draft's send is over (Sent or Not sent): its send request, if one is open, is answered here like any other. */
	let emailEnded = $state(false);
	/** The last read failed and none has succeeded since: the bar does not know the artifact's requests. */
	let readFailed = $state(false);
	let retrying = $state(false);
	const current = (id: string, stamp: number) => artifactId === id && generation === stamp;

	const noteRequired = () => noteDemanded || (read?.round_feedback_count ?? 0) === 0;
	const visible = () => phase === 'answered' || phase === 'stale' || phase === 'sending' || Boolean(read?.ask_raised);
	/** An email draft whose send is over and whose requests could not be read: the bar says so and offers to read again. Until the email ends, the send bar is the one that speaks. */
	const unreadable = () => emailEnded && read === null && readFailed;
	/** An email draft whose send is over and whose open request is on screen with answers: the send bar may point the person here. */
	const answersOpen = () => emailEnded && (phase === 'ask' || phase === 'noting') && Boolean(read?.ask_raised) && answerHoldIds(read, true).length > 0;

	function setTarget(id: string | null): boolean {
		if (id === artifactId) return false;
		artifactId = id; generation += 1; loadSequence += 1; settledAsk = null;
		read = null; phase = 'ask'; note = ''; noteFor = 'changes_requested'; failure = ''; receipt = null; noteDemanded = false; readFailed = false; retrying = false;
		return true;
	}
	async function load() {
		const id = artifactId, stamp = generation, sequence = ++loadSequence;
		if (!id) return;
		try {
			const next = await fetchReviewBar(id);
			if (!current(id, stamp) || sequence !== loadSequence) return;
			read = next; readFailed = false;
			if ((phase === 'answered' || phase === 'stale') && next.ask_raised && next.ask_raised_at !== settledAsk) {
				phase = 'ask'; receipt = null; failure = ''; note = ''; settledAsk = null; noteDemanded = false;
			} else if (phase === 'noting' && !next.ask_raised) {
				phase = 'ask'; note = '';
			}
		} catch {
			// The bar keeps what it last knew; the next change event reads again. With nothing known it says so once the email has ended (`unreadable`).
			if (current(id, stamp) && sequence === loadSequence) readFailed = true;
		}
	}
	/** Read again, and only read: no verdict is sent from here. */
	async function retryRead() {
		if (retrying) return;
		retrying = true;
		try { await load(); } finally { retrying = false; }
	}
	function openNote() { if (phase === 'ask') { failure = ''; phase = 'noting'; } }
	function cancelNote() { if (phase === 'noting') { note = ''; failure = ''; phase = 'ask'; } }

	/** Send the verdict; `from` is where a failed send returns to, with the note kept. */
	async function answer(
		verdict: ReviewVerdict,
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
			const sent = await answerReview(id, verdict, verdict !== 'approved' ? note : '', answerHoldIds(read, emailEnded));
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
				// The note opens for the verdict that was refused, so sending it records that verdict.
				if (verdict !== 'approved') noteFor = verdict;
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
		get noteFor() { return noteFor; }, set noteFor(value: typeof noteFor) { noteFor = value; },
		get emailEnded() { return emailEnded; }, set emailEnded(value: boolean) { emailEnded = value; },
		get accepting() { return accepting; }, set accepting(value: boolean) { accepting = value; },
		get failure() { return failure; }, set failure(value: string) { failure = value; },
		get visible() { return visible(); },
		get unreadable() { return unreadable(); }, get answersOpen() { return answersOpen(); }, get retrying() { return retrying; },
		get noteRequired() { return noteRequired(); },
		get artifactId() { return artifactId; },
		setTarget, load, retryRead, openNote, cancelNote, answer,
	};
}
export type ReviewBarState = ReturnType<typeof createReviewBarState>;
