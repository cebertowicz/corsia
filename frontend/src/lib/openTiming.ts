/**
 * Click-to-usable timing for thread and artifact opens (issue #2280).
 *
 * The server half of a slow open is already measurable from request timing
 * (#3274); these two client events carry the half that is not: how long from
 * the user's action until the surface is usable, and how much of that is the
 * render rather than the fetch.
 *
 * This module is deliberately pure — no stores, no DOM, no analytics import —
 * so the state machine is testable in isolation. `openTimingRuntime.ts` owns
 * the app singleton and binds it to `emitProductEvent` and `performance.now()`.
 *
 * Nothing here identifies a thread, a document or a title: emitted properties
 * are durations, a bounded outcome, a bounded source, a count bucket and an
 * artifact kind. The thread id is held only to attribute a mark to the open
 * that started it.
 */

export type ThreadOpenSource = 'sidebar' | 'link' | 'history' | 'other';
export type OpenOutcome = 'settled' | 'abandoned' | 'error';
export type MessageCountBucket = '0' | '1-50' | '51-500' | '500+';

export interface OpenTimingSink {
	/** Monotonic-ish clock; the only time source this module trusts. */
	now(): number;
	emit(category: string, action: string, properties: Record<string, unknown>): void;
}

interface ThreadOpenState {
	threadId: string;
	source: ThreadOpenSource;
	startedAt: number;
	firstPageMs: number | null;
	loadedCount: number;
	/**
	 * A pane that was on screen when the first page landed still owes this open its
	 * artifact list. False means nothing on screen ever will, so the open does not
	 * wait for one (#2280).
	 */
	awaitsArtifacts: boolean;
	/**
	 * Whether this open ever waited on an artifact list, which stays true if the pane
	 * that owed it left again. Reported as-is: an open that waited and an open that
	 * never did settle for different reasons, and a mixed percentile hides that.
	 */
	artifactsAwaited: boolean;
	artifactsLoaded: boolean;
}

interface ArtifactOpenState {
	instanceId: string;
	kind: string;
	startedAt: number;
}

/** Bucket of the messages actually loaded for the open, never a server total. */
export function messageCountBucket(count: number): MessageCountBucket {
	if (!Number.isFinite(count) || count <= 0) return '0';
	if (count <= 50) return '1-50';
	if (count <= 500) return '51-500';
	return '500+';
}

export function createOpenTiming(sink: OpenTimingSink) {
	let threadOpen: ThreadOpenState | null = null;
	let artifactOpen: ArtifactOpenState | null = null;
	/** Artifact panes currently on screen, by the thread each one is showing. */
	const mountedPanes = new Map<string, number>();

	function emitThreadOpen(state: ThreadOpenState, outcome: OpenOutcome, settledMs: number | null): void {
		sink.emit('navigation', 'thread_open', {
			click_to_first_page_ms: state.firstPageMs,
			click_to_settled_ms: settledMs,
			outcome,
			source: state.source,
			artifacts_awaited: state.artifactsAwaited,
			message_count_bucket: messageCountBucket(state.loadedCount),
		});
	}

	/**
	 * Settles when the first message page rendered and nothing on screen still owes
	 * the open an artifact list. A pane that is not mounted cannot report one, so
	 * waiting for it would end every such open as "abandoned" at the next click.
	 */
	function settleThreadIfReady(): void {
		const state = threadOpen;
		if (!state || state.firstPageMs === null) return;
		if (state.awaitsArtifacts && !state.artifactsLoaded) return;
		threadOpen = null;
		emitThreadOpen(state, 'settled', sink.now() - state.startedAt);
	}

	/**
	 * Starts the clock. Idempotent for the open already pending on this thread: the
	 * route begins it before its awaits and the panel begins it again once the
	 * selection is real, and only the first of those may decide when it began.
	 */
	function beginThreadOpen(threadId: string, source: ThreadOpenSource = 'other'): void {
		if (threadOpen?.threadId === threadId) return;
		if (threadOpen) {
			const abandoned = threadOpen;
			threadOpen = null;
			emitThreadOpen(abandoned, 'abandoned', null);
		}
		threadOpen = {
			threadId,
			source,
			startedAt: sink.now(),
			firstPageMs: null,
			loadedCount: 0,
			awaitsArtifacts: false,
			artifactsAwaited: false,
			artifactsLoaded: false,
		};
	}

	/**
	 * The first message page is in the DOM; `loadedCount` is what it rendered, and
	 * `artifactsPaneMounted` says whether a pane on screen still owes the list.
	 */
	function markFirstPageRendered(
		threadId: string, loadedCount: number, artifactsPaneMounted: boolean,
	): void {
		const state = threadOpen;
		if (!state || state.threadId !== threadId || state.firstPageMs !== null) return;
		state.firstPageMs = sink.now() - state.startedAt;
		state.loadedCount = loadedCount;
		state.artifactsAwaited = artifactsPaneMounted;
		state.awaitsArtifacts = artifactsPaneMounted;
		settleThreadIfReady();
	}

	/** The thread's artifact list has loaded, or is known to be empty. */
	function markArtifactsLoaded(threadId: string): void {
		const state = threadOpen;
		if (!state || state.threadId !== threadId) return;
		state.artifactsLoaded = true;
		settleThreadIfReady();
	}

	/**
	 * The last pane showing this thread left the screen while its open was still
	 * pending: nothing will report the list now, so the open stops waiting for it.
	 */
	function noteArtifactsPaneGone(threadId: string): void {
		const state = threadOpen;
		if (!state || state.threadId !== threadId || !state.awaitsArtifacts) return;
		state.awaitsArtifacts = false;
		settleThreadIfReady();
	}

	/** Whether a pane showing this thread is on screen right now. */
	function artifactsPaneMounted(threadId: string): boolean {
		return mountedPanes.has(threadId);
	}

	/**
	 * An artifact pane reached the screen for `threadId`. Call the returned release
	 * when it leaves or starts showing another thread.
	 */
	function registerArtifactsPane(threadId: string): () => void {
		mountedPanes.set(threadId, (mountedPanes.get(threadId) ?? 0) + 1);
		return () => {
			const remaining = (mountedPanes.get(threadId) ?? 1) - 1;
			if (remaining > 0) {
				mountedPanes.set(threadId, remaining);
				return;
			}
			mountedPanes.delete(threadId);
			noteArtifactsPaneGone(threadId);
		};
	}

	function failThreadOpen(threadId: string): void {
		const state = threadOpen;
		if (!state || state.threadId !== threadId) return;
		threadOpen = null;
		emitThreadOpen(state, 'error', null);
	}

	/**
	 * The displayed thread changed. An open can only settle while its thread is
	 * still the one on screen, so any other selection (or none) abandons it.
	 */
	function noteDisplayedThread(threadId: string | null): void {
		const state = threadOpen;
		if (!state || state.threadId === threadId) return;
		threadOpen = null;
		emitThreadOpen(state, 'abandoned', null);
	}

	function emitArtifactOpen(state: ArtifactOpenState, outcome: OpenOutcome, contentMs: number | null): void {
		sink.emit('navigation', 'artifact_open', {
			click_to_content_ms: contentMs,
			outcome,
			kind: state.kind,
		});
	}

	function beginArtifactOpen(instanceId: string, kind: string): void {
		if (artifactOpen) {
			const abandoned = artifactOpen;
			artifactOpen = null;
			emitArtifactOpen(abandoned, 'abandoned', null);
		}
		artifactOpen = { instanceId, kind, startedAt: sink.now() };
	}

	/** The artifact's content is on screen for the tab that asked for it. */
	function markArtifactRendered(instanceId: string): void {
		const state = artifactOpen;
		if (!state || state.instanceId !== instanceId) return;
		artifactOpen = null;
		emitArtifactOpen(state, 'settled', sink.now() - state.startedAt);
	}

	function failArtifactOpen(instanceId: string): void {
		const state = artifactOpen;
		if (!state || state.instanceId !== instanceId) return;
		artifactOpen = null;
		emitArtifactOpen(state, 'error', null);
	}

	function abandonArtifactOpen(): void {
		const state = artifactOpen;
		if (!state) return;
		artifactOpen = null;
		emitArtifactOpen(state, 'abandoned', null);
	}

	return {
		beginThreadOpen,
		markFirstPageRendered,
		markArtifactsLoaded,
		failThreadOpen,
		noteDisplayedThread,
		artifactsPaneMounted,
		registerArtifactsPane,
		beginArtifactOpen,
		markArtifactRendered,
		failArtifactOpen,
		abandonArtifactOpen,
	};
}