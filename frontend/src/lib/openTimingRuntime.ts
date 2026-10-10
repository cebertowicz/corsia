/**
 * The app singleton for open timing (#2280).
 *
 * Keeps `openTiming.ts` pure: this is the only place the state machine is bound
 * to the product-event wrapper, a real clock, and the displayed-thread store.
 * Emission stays fail-silent because `emitProductEvent` is — a timing emit can
 * never break a navigation.
 */
import { activeThreadId } from '$lib/stores/threads';
import { emitProductEvent } from '$lib/product_events';
import { createOpenTiming } from '$lib/openTiming';

export type { ThreadOpenSource } from '$lib/openTiming';

const timing = createOpenTiming({
	now: () => (typeof performance !== 'undefined' ? performance.now() : Date.now()),
	emit: (category, action, properties) => emitProductEvent(category, action, properties),
});

export const beginThreadOpen = timing.beginThreadOpen;
export const markArtifactsLoaded = timing.markArtifactsLoaded;
export const failThreadOpen = timing.failThreadOpen;
export const beginArtifactOpen = timing.beginArtifactOpen;
export const markArtifactRendered = timing.markArtifactRendered;
export const failArtifactOpen = timing.failArtifactOpen;
export const abandonArtifactOpen = timing.abandonArtifactOpen;

/**
 * An artifact pane reports its own life here, so the machine knows whether a list
 * is still owed. `demo` panes and panes with no thread have nothing to report.
 */
export function registerArtifactsPane(threadId: string | null | undefined): () => void {
	if (!threadId) return () => {};
	return timing.registerArtifactsPane(threadId);
}

/**
 * The first message page is in the DOM. The machine is told whether a mounted
 * pane still owes the open its artifact list, which is what decides if it waits.
 */
export function markFirstPageRendered(threadId: string, loadedCount: number): void {
	timing.markFirstPageRendered(threadId, loadedCount, timing.artifactsPaneMounted(threadId));
}

// One subscription for the app's lifetime: the displayed thread is the single
// fact that says whether a pending open can still be observed settling, so no
// surface has to remember to report that the user left.
activeThreadId.subscribe((id) => timing.noteDisplayedThread(id));