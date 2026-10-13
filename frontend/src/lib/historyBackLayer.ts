/**
 * A stack of dismissible UI layers that hardware/gesture Back can close.
 *
 * Extracted from the pattern `Lightbox.svelte` already shipped, because C0
 * needs it in two more places (the compact artifact surface and the tab sheet)
 * and they must nest: pressing Back with the sheet open closes the sheet only,
 * and pressing it again returns to chat. Duplicating the raw
 * pushState/popstate dance per component cannot express that ordering.
 *
 * Each layer pushes one history entry. The most recently pushed layer gets
 * first refusal on a pop; returning true consumes it so the browser never
 * leaves the app while a layer is still open.
 */

interface BackLayer {
	id: number;
	onPop: () => boolean;
	/** True once this layer has a corresponding browser-history entry. */
	pushed: boolean;
	/**
	 * Dismissed while a layer above it was still open. Its history entry sits under that layer's, so it stays on the
	 * stack until that layer goes, and the two entries are unwound together.
	 */
	left?: boolean;
}

let layers: BackLayer[] = [];
let nextId = 1;
let listening = false;

interface PendingCleanup {
	/**
	 * The states the direct dismissals' history.back() calls are expected to land on, in order. Two top layers can be
	 * dismissed in one flush (a sheet's pick closes the sheet and the layer beneath it), and each traversal arrives on
	 * its own.
	 */
	expectedLowerLayerIds: Array<number | null>;
	expires: ReturnType<typeof setTimeout>;
}

const CLEANUP_POP_GRACE_MS = 1_000;

// A direct UI dismissal removes its layer before history.back() delivers its
// popstate. Keep one bounded, state-matched cleanup expectation — not a stale
// counter that can consume a replacement layer's real Back.
let pendingCleanup: PendingCleanup | null = null;

function clearPendingCleanup(): void {
	if (!pendingCleanup) return;
	clearTimeout(pendingCleanup.expires);
	pendingCleanup = null;
}

function flushDeferredLayers(): void {
	for (const layer of layers) {
		if (layer.pushed) continue;
		history.pushState({ backLayer: layer.id }, '');
		layer.pushed = true;
	}
}

/**
 * Drops the dismissed layers now at the top of the stack and traverses back past their history entries, plus
 * `entries` more, in one step.
 */
function unwindLeftLayers(entries: number): void {
	while (layers.at(-1)?.left) {
		layers = layers.slice(0, -1);
		entries += 1;
	}
	if (!entries) return;
	const lowerPushed = [...layers].reverse().find((candidate) => candidate.pushed);
	expectCleanup(lowerPushed?.id ?? null);
	if (entries === 1) history.back();
	else history.go(-entries);
}

function expectCleanup(expectedLowerLayerId: number | null): void {
	const earlier = pendingCleanup?.expectedLowerLayerIds ?? [];
	clearPendingCleanup();
	const pending: PendingCleanup = {
		expectedLowerLayerIds: [...earlier, expectedLowerLayerId],
		expires: setTimeout(() => {
			if (pendingCleanup !== pending) return;
			pendingCleanup = null;
			flushDeferredLayers();
		}, CLEANUP_POP_GRACE_MS),
	};
	pendingCleanup = pending;
}

function historyLayerId(state: unknown): number | null {
	if (!state || typeof state !== 'object') return null;
	const id = (state as { backLayer?: unknown }).backLayer;
	return typeof id === 'number' ? id : null;
}

function handlePopState(event: PopStateEvent): void {
	if (pendingCleanup) {
		const [expected, ...later] = pendingCleanup.expectedLowerLayerIds;
		// A lower live layer gives the cleanup pop a stable identity even if a
		// replacement was pushed before this event reached us. With no lower layer
		// the first pop during the short cleanup grace is necessarily the direct
		// history.back() cleanup, even if a replacement was pushed in the same flush.
		if (expected === null || historyLayerId(event.state) === expected) {
			event.stopImmediatePropagation();
			if (later.length) {
				pendingCleanup.expectedLowerLayerIds = later;
				return;
			}
			clearPendingCleanup();
			flushDeferredLayers();
			return;
		}
		// This is a real pop, not the deferred cleanup; do not retain stale state.
		clearPendingCleanup();
		flushDeferredLayers();
	}
	const top = layers[layers.length - 1];
	if (!top) return;
	// The entry this layer pushed is already gone; drop it before dispatching so
	// a handler that re-enters cannot see a stale stack.
	layers = layers.slice(0, -1);
	if (top.left || top.onPop()) event.stopImmediatePropagation();
	// The layer that just closed may have had dismissed ones beneath it; Back never stops on their entries.
	unwindLeftLayers(0);
}

function ensureListener(): void {
	if (listening || typeof window === 'undefined') return;
	window.addEventListener('popstate', handlePopState);
	listening = true;
}

/**
 * Pushes a back layer. Returns a disposer that removes it, unwinding the
 * history entry it added when it is still the top of the stack, or once the
 * layers above it have closed when it is not.
 */
export function pushBackLayer(onPop: () => boolean): () => void {
	if (typeof window === 'undefined') return () => {};
	ensureListener();
	const layer: BackLayer = { id: nextId++, onPop, pushed: false };
	layers = [...layers, layer];
	// A direct dismissal's history.back() is asynchronous. Pushing a replacement
	// before that traversal lands can orphan the replacement entry and make its
	// later cleanup rewind past the app. Register it now, then publish its history
	// entry only after the pending cleanup pop has been consumed.
	if (!pendingCleanup) {
		history.pushState({ backLayer: layer.id }, '');
		layer.pushed = true;
	}
	return () => {
		const idx = layers.findIndex((l) => l.id === layer.id);
		if (idx < 0 || layer.left) return; // already consumed by a pop, or already dismissed
		const wasTop = idx === layers.length - 1;
		// A buried layer's entry cannot be removed from under the open one above it.
		if (!wasTop && layer.pushed) {
			layer.left = true;
			return;
		}
		layers = layers.filter((l) => l.id !== layer.id);
		// Only a top layer with a published history entry owns the current browser
		// entry. A deferred replacement can disappear without traversing history.
		if (wasTop) unwindLeftLayers(layer.pushed ? 1 : 0);
	};
}

/** Test seam — drops all layers without touching history. */
export function resetBackLayers(): void {
	layers = [];
	clearPendingCleanup();
}

/** Test seam — how many layers are currently open. */
export function backLayerDepth(): number {
	return layers.filter((layer) => !layer.left).length;
}
