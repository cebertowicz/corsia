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
}

let layers: BackLayer[] = [];
let nextId = 1;
let listening = false;

interface PendingCleanup {
	/** The state a direct dismissal's history.back() is expected to land on. */
	expectedLowerLayerId: number | null;
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

function expectCleanup(expectedLowerLayerId: number | null): void {
	clearPendingCleanup();
	const pending: PendingCleanup = {
		expectedLowerLayerId,
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
		const expected = pendingCleanup.expectedLowerLayerId;
		// A lower live layer gives the cleanup pop a stable identity even if a
		// replacement was pushed before this event reached us.
		if (expected !== null && historyLayerId(event.state) === expected) {
			clearPendingCleanup();
			event.stopImmediatePropagation();
			flushDeferredLayers();
			return;
		}
		// With no lower layer the first pop during the short cleanup grace is
		// necessarily the direct history.back() cleanup, even if a replacement was
		// pushed in the same flush.
		if (expected === null) {
			clearPendingCleanup();
			event.stopImmediatePropagation();
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
	if (top.onPop()) event.stopImmediatePropagation();
}

function ensureListener(): void {
	if (listening || typeof window === 'undefined') return;
	window.addEventListener('popstate', handlePopState);
	listening = true;
}

/**
 * Pushes a back layer. Returns a disposer that removes it, unwinding the
 * history entry it added when it is still the top of the stack.
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
		if (idx < 0) return; // already consumed by a pop
		const wasTop = idx === layers.length - 1;
		const wasPushed = layer.pushed;
		layers = layers.filter((l) => l.id !== layer.id);
		// Only a top layer with a published history entry owns the current browser
		// entry. A deferred replacement can disappear without traversing history.
		if (wasTop && wasPushed) {
			const lowerPushed = [...layers].reverse().find((candidate) => candidate.pushed);
			expectCleanup(lowerPushed?.id ?? null);
			history.back();
		}
	};
}

/** Test seam — drops all layers without touching history. */
export function resetBackLayers(): void {
	layers = [];
	clearPendingCleanup();
}

/** Test seam — how many layers are currently registered. */
export function backLayerDepth(): number {
	return layers.length;
}
