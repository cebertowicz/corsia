import {
	answerReview, fetchReviewBar, ReviewRefusal,
	type FeedbackCounts, type ReviewBarRead, type ReviewVerdict,
} from '$lib/api/reviewBar';

export type ReviewBarPhase = 'ask' | 'noting' | 'sending' | 'answered' | 'stale';
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
	let failure = $state('');
	let receipt = $state<ReviewReceipt | null>(null);
	const current = (id: string, stamp: number) => artifactId === id && generation === stamp;

	/** The label follows the round's feedback, never the last click. */
	const approveLabel = () => ((read?.round_feedback_count ?? 0) > 0 ? 'Approve with changes' : 'Approve');
	const noteRequired = () => (read?.round_feedback_count ?? 0) === 0;
	const visible = () => phase === 'answered' || phase === 'stale' || phase === 'sending' || Boolean(read?.ask_raised);

	function setTarget(id: string | null): boolean {
		if (id === artifactId) return false;
		artifactId = id; generation += 1; loadSequence += 1; settledAsk = null;
		read = null; phase = 'ask'; note = ''; failure = ''; receipt = null;
		return true;
	}
	async function load() {
		const id = artifactId, stamp = generation, sequence = ++loadSequence;
		if (!id) return;
		try {
			const next = await fetchReviewBar(id);
			if (!current(id, stamp) || sequence !== loadSequence) return;
			read = next;
			if ((phase === 'answered' || phase === 'stale') && next.ask_raised && next.ask_raised_at !== settledAsk) {
				phase = 'ask'; receipt = null; failure = ''; note = ''; settledAsk = null;
			} else if (phase === 'noting' && !next.ask_raised) {
				phase = 'ask'; note = '';
			}
		} catch { /* The bar keeps what it last knew; the next change event reads again. */ }
	}
	function openNote() { if (phase === 'ask') { failure = ''; phase = 'noting'; } }
	function cancelNote() { if (phase === 'noting') { note = ''; failure = ''; phase = 'ask'; } }

	/** Send the verdict; `from` is where a failed send returns to, with the note kept. */
	async function answer(verdict: ReviewVerdict) {
		const id = artifactId, stamp = generation;
		if (!id || (phase !== 'ask' && phase !== 'noting')) return false;
		if (verdict === 'changes_requested' && noteRequired() && !note.trim()) { failure = NOTE_NEEDED; return false; }
		const from = phase;
		sendingFrom = from;
		phase = 'sending'; failure = '';
		try {
			const sent = await answerReview(id, verdict, verdict === 'changes_requested' ? note : '');
			if (!current(id, stamp)) return false;
			const counts = sent.feedback_counts;
			settledAsk = read?.ask_raised_at ?? null;
			receipt = { verdict: sent.verdict, sent: counts, at: new Date() };
			phase = 'answered';
			return true;
		} catch (error) {
			if (!current(id, stamp)) return false;
			if (error instanceof ReviewRefusal && error.code === 'ask_stale') {
				settledAsk = read?.ask_raised_at ?? null;
				phase = 'stale';
				await load();
			} else if (error instanceof ReviewRefusal && error.code === 'note_required') {
				phase = 'noting'; failure = NOTE_NEEDED;
			} else {
				phase = from; failure = SEND_FAILED;
			}
			return false;
		}
	}
	return {
		get read() { return read; }, get phase() { return phase; }, get sendingFrom() { return sendingFrom; }, get receipt() { return receipt; },
		get note() { return note; }, set note(value: string) { note = value; },
		get failure() { return failure; }, set failure(value: string) { failure = value; },
		get visible() { return visible(); }, get approveLabel() { return approveLabel(); },
		get noteRequired() { return noteRequired(); },
		get artifactId() { return artifactId; },
		setTarget, load, openNote, cancelNote, answer,
	};
}
export type ReviewBarState = ReturnType<typeof createReviewBarState>;
