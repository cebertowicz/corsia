import {
	fetchDocumentReviews, requestDocumentReview, cancelDocumentReview, reviewFailure,
	type DocumentReview, type DocumentReviewer,
} from '$lib/api/docReviews';

export const andList = (list: string[]) => list.length < 3 ? list.join(' and ') : `${list.slice(0, -1).join(', ')} and ${list[list.length - 1]}`;

/** Document-local request state; every asynchronous result belongs to a full target generation. */
export function createDocumentReviewState() {
	let artifactId: string | null = null;
	let threadId: string | null = null;
	let targetKey: string | null = null;
	let generation = 0;
	let loadSequence = 0;
	let requests = $state<DocumentReview[]>([]);
	let reviewers = $state<DocumentReviewer[]>([]);
	let loadState = $state<'loading' | 'ready' | 'error'>('loading');
	let rosterError = $state(false);
	let selected = $state<DocumentReviewer[]>([]);
	let lastSent = $state<string[]>([]);
	let note = $state('');
	let sending = $state(false);
	let refusal = $state('');
	/** Why each chosen reviewer's request did not go through, by reviewer id; they stay chosen so Retry resends only them. */
	let failures = $state<Record<string, string>>({});
	let cancelPending = $state<string[]>([]);
	let cancelErrors = $state<Record<string, string>>({});
	// One retry key per reviewer and note, so a retry after a partial failure never repeats a request already made.
	let attempts = new Map<string, string>();
	const current = (id: string, thread: string | null, stamp: number) => artifactId === id && threadId === thread && generation === stamp;

	function setTarget(id: string | null, thread: string | null, key: string | null) {
		if (id === artifactId && thread === threadId && key === targetKey) return false;
		artifactId = id; threadId = thread; targetKey = key; generation += 1; loadSequence += 1;
		requests = []; reviewers = []; loadState = 'loading'; rosterError = false;
		selected = []; lastSent = []; note = ''; sending = false; refusal = ''; failures = {}; cancelPending = []; cancelErrors = {}; attempts = new Map();
		return true;
	}
	async function load() {
		const id = artifactId, thread = threadId, stamp = generation, sequence = ++loadSequence;
		if (!id) return;
		loadState = 'loading';
		try {
			const response = await fetchDocumentReviews(id, thread);
			if (!current(id, thread, stamp) || sequence !== loadSequence) return;
			requests = response.reviews;
			rosterError = response.reviewers_error === 'roster_error';
			reviewers = response.reviewers ?? [];
			loadState = 'ready';
		} catch {
			if (current(id, thread, stamp) && sequence === loadSequence) loadState = 'error';
		}
	}
	/** The agents a person may add: not already requested, not chosen, and able to take the work now. */
	function choices() {
		return reviewers.filter((reviewer) => !reviewer.requested && !reviewer.unavailable_reason && !selected.some((chosen) => chosen.id === reviewer.id));
	}
	function select(reviewer: DocumentReviewer) {
		if (!selected.some((chosen) => chosen.id === reviewer.id)) selected = [...selected, reviewer];
		if (!Object.keys(failures).length) refusal = '';
	}
	function deselect(id: string) {
		selected = selected.filter((reviewer) => reviewer.id !== id);
		const { [id]: _dropped, ...rest } = failures; failures = rest;
		if (!Object.keys(failures).length) refusal = '';
	}
	/** The request route takes one reviewer per call, so each chosen reviewer is its own request; those that succeed leave the selection. */
	async function submit(): Promise<boolean> {
		const id = artifactId, thread = threadId, stamp = generation, chosen = [...selected];
		if (!id || !thread || !chosen.length || sending || note.length > 500) return false;
		sending = true; refusal = ''; lastSent = []; failures = {};
		const sent: DocumentReviewer[] = [];
		const failed: Record<string, string> = {};
		try {
			for (const reviewer of chosen) {
				const key = JSON.stringify([reviewer.id, note]);
				if (!attempts.has(key)) attempts.set(key, crypto.randomUUID());
				try {
					await requestDocumentReview(id, thread, reviewer.id, note, attempts.get(key)!);
					if (!current(id, thread, stamp)) return false;
					sent.push(reviewer);
					attempts.delete(key);
				} catch (error) {
					if (!current(id, thread, stamp)) return false;
					failed[reviewer.id] = reviewFailure(error, "Couldn't send the request. Try again.");
				}
			}
			if (!current(id, thread, stamp)) return false;
			selected = selected.filter((reviewer) => !sent.some((done) => done.id === reviewer.id));
			lastSent = sent.map((reviewer) => reviewer.name);
			// The list owns display names and cancellation rights; never derive them from a storage row.
			if (sent.length) await load();
			if (Object.keys(failed).length) {
				failures = failed;
				refusal = sent.length ? `Requested from ${andList(lastSent)}. These didn't go through:` : "Couldn't send the review request:";
				return false;
			}
			note = '';
			return current(id, thread, stamp);
		} finally { if (current(id, thread, stamp)) sending = false; }
	}
	async function cancel(id: string) {
		const artifact = artifactId, thread = threadId, stamp = generation;
		if (!artifact || cancelPending.includes(id)) return;
		cancelPending = [...cancelPending, id]; delete cancelErrors[id];
		try {
			await cancelDocumentReview(id);
			if (!current(artifact, thread, stamp)) return;
			requests = requests.filter((request) => request.id !== id);
			await load();
		} catch (error) {
			if (current(artifact, thread, stamp)) cancelErrors[id] = reviewFailure(error, "Couldn't cancel this request. Try again.");
		} finally { if (current(artifact, thread, stamp)) cancelPending = cancelPending.filter((request) => request !== id); }
	}
	return {
		get requests() { return requests; }, get reviewers() { return reviewers; },
		get loadState() { return loadState; }, get rosterError() { return rosterError; },
		get selected() { return selected; }, get lastSent() { return lastSent; },
		get note() { return note; }, set note(value: string) { note = value; },
		get sending() { return sending; }, get refusal() { return refusal; }, get failures() { return failures; },
		get cancelPending() { return cancelPending; }, get cancelErrors() { return cancelErrors; },
		get artifactId() { return artifactId; },
		setTarget, load, submit, cancel, choices, select, deselect,
	};
}
export type DocumentReviewState = ReturnType<typeof createDocumentReviewState>;
