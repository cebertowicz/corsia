import {
	answerReview, fetchReviewBar, ReviewRefusal,
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
	let mounted = false;
	let retryTimer: ReturnType<typeof setTimeout> | undefined;
	let failures = 0;
	let lastLoadFailed = false;
	const retryDelays = [1000, 2000, 4000, 8000, 16000, 30000];
	function cancelRetry() { clearTimeout(retryTimer); retryTimer = undefined; }
	function scheduleRetry() {
		if (!mounted || document.visibilityState !== 'visible' || !artifactId) return;
		cancelRetry();
		const delay = retryDelays[Math.min(failures - 1, retryDelays.length - 1)];
		retryTimer = setTimeout(() => void load(), delay);
	}
	function activate() { mounted = true; if (lastLoadFailed) scheduleRetry(); }
	function deactivate() { mounted = false; cancelRetry(); loadSequence += 1; }
	function suspendRetry() { cancelRetry(); }
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
	const current = (id: string, stamp: number) => artifactId === id && generation === stamp;

	const noteRequired = () => noteDemanded || (read?.round_feedback_count ?? 0) === 0;
	const visible = () => phase === 'answered' || phase === 'stale' || phase === 'sending' || Boolean(read?.ask_raised);

	function setTarget(id: string | null): boolean {
		if (id === artifactId) return false;
		cancelRetry(); failures = 0; lastLoadFailed = false;
		artifactId = id; generation += 1; loadSequence += 1; settledAsk = null;
		read = null; phase = 'ask'; note = ''; noteFor = 'changes_requested'; failure = ''; receipt = null; noteDemanded = false;
		return true;
	}
	async function load() {
		cancelRetry();
		const id = artifactId, stamp = generation, sequence = ++loadSequence;
		if (!id) return;
		try {
			const next = await fetchReviewBar(id);
			if (!current(id, stamp) || sequence !== loadSequence) return;
			failures = 0; lastLoadFailed = false;
			read = next;
			if ((phase === 'answered' || phase === 'stale') && next.ask_raised && next.ask_raised_at !== settledAsk) {
				phase = 'ask'; receipt = null; failure = ''; note = ''; settledAsk = null; noteDemanded = false;
			} else if (phase === 'noting' && !next.ask_raised) {
				phase = 'ask'; note = '';
			}
		} catch {
			if (!current(id, stamp) || sequence !== loadSequence) return;
			lastLoadFailed = true; failures += 1; scheduleRetry();
		}
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
		get accepting() { return accepting; }, set accepting(value: boolean) { accepting = value; },
		get failure() { return failure; }, set failure(value: string) { failure = value; },
		get visible() { return visible(); },
		get noteRequired() { return noteRequired(); },
		get artifactId() { return artifactId; },
		setTarget, load, activate, deactivate, suspendRetry, openNote, cancelNote, answer,
	};
}
export type ReviewBarState = ReturnType<typeof createReviewBarState>;
