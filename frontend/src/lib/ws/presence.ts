/**
 * User presence client (issue #2004): tells the server, over the existing
 * socket, whether the conversation a view shows is actually in front of the
 * user.
 *
 * A view calls `startPresence(threadId)` while it displays that thread and
 * `stopPresence()` when it stops. The claim (`foreground`) is sent only after
 * the thread has been shown for 3 s in a visible page with user input in the
 * last 10 min. Leaving sends `background` at once (tagged `hidden` for a
 * hidden page; the server applies the 30 s hidden grace), and 10 min without
 * input sends `idle` with how long ago the last input was. Foreground frames
 * add their focus state for Measurement only; shared presence stays unchanged.
 * Frames carry state, never client clock times, and are re-sent on every reconnect.
 *
 * Presence stays on the CORSIA server: this module never sends anything to
 * product analytics.
 */
import { get, writable } from 'svelte/store';
import { connectionState } from '../stores/connection';
import { lastUserInputAt, onUserInput } from '../userInput';
import { ws } from './state';
import type { DocumentOpenCapability } from '$lib/artifactDocumentOpenCapability';

export const DWELL_MS = 3_000;
export const IDLE_CAP_MS = 10 * 60_000;

export type PresenceSurface = 'chat';

export type PresenceFrame = {
	type: 'presence';
	surface: PresenceSurface;
	thread_id: string;
} & (
	| { state: 'foreground'; focused?: boolean }
	| { state: 'background'; reason?: 'hidden' }
	| { state: 'idle'; idle_for_ms: number }
	| {
		state: 'document_open_capability';
		document_open: { can_show: boolean; visible_artifact_id: string | null };
	}
);

export interface PresenceEnv {
	send(frame: PresenceFrame): void;
	now(): number;
	/** The page input clock's last input; -Infinity before any. */
	lastInputAt(): number;
	isVisible(): boolean;
	isFocused(): boolean;
	setTimer(fn: () => void, ms: number): unknown;
	clearTimer(handle: unknown): void;
}

export interface PresenceTracker {
	/** The view now displays `threadId` on `surface`. */
	show(threadId: string, surface?: PresenceSurface): void;
	/** The view no longer displays any thread. */
	hide(): void;
	/** The page's visibility changed. */
	visibilityChanged(): void;
	/** The page input clock moved: re-read it. */
	input(): void;
	/** The page's focus target changed: update Measurement without releasing presence. */
	focusChanged(): void;
	/** A new socket authenticated: re-send the current claim. */
	reconnected(): void;
	/** Update document delivery facts only when the composed canvas changes them. */
	updateDocumentOpenCapability(capability: DocumentOpenCapability): void;
	/** Latest document-open fact this page published with its presence claim. */
	readonly documentOpenCapability: DocumentOpenCapability | null;
	/** Displayed thread while a chat presence target exists. */
	readonly displayedThreadId: string | null;
	/** Whether a claim is currently asserted. */
	readonly claimed: boolean;
	/** Claimed thread while asserted; null after release. */
	readonly claimedThreadId: string | null;
}

export function createPresenceTracker(env: PresenceEnv): PresenceTracker {
	let target: { threadId: string; surface: PresenceSurface } | null = null;
	let claimed = false;
	let shownSince: number | null = null;
	let dwellTimer: unknown = null;
	let idleTimer: unknown = null;
	let documentOpen: DocumentOpenCapability | null = null;
	let focused: boolean | null = null;

	const frame = (extra: Record<string, unknown>): PresenceFrame => ({
		type: 'presence',
		surface: target!.surface,
		thread_id: target!.threadId,
		...extra,
	} as PresenceFrame);

	const clear = (timer: unknown) => {
		if (timer !== null) env.clearTimer(timer);
	};

	const inputIsRecent = () => env.now() - env.lastInputAt() < IDLE_CAP_MS;

	function armIdle() {
		clear(idleTimer);
		idleTimer = null;
		if (!claimed) return;
		idleTimer = env.setTimer(goIdle, Math.max(0, env.lastInputAt() + IDLE_CAP_MS - env.now()));
	}

	function goIdle() {
		idleTimer = null;
		if (!claimed || !target) return;
		const idleFor = env.now() - env.lastInputAt();
		if (idleFor < IDLE_CAP_MS) return armIdle();
		claimed = false;
		env.send(frame({ state: 'idle', idle_for_ms: idleFor }));
	}

	function publishDocumentOpenCapability() {
		if (!claimed || !documentOpen) return;
		env.send(frame({
			state: 'document_open_capability',
			document_open: {
				can_show: documentOpen.canShow,
				visible_artifact_id: documentOpen.showing,
			},
		}));
	}

	function publishFocus() {
		if (!claimed || !target) return;
		const next = env.isFocused();
		if (focused === next) return;
		focused = next;
		env.send(frame({ state: 'foreground', focused: next }));
	}

	function tryClaim() {
		dwellTimer = null;
		if (claimed || !target || !env.isVisible() || !inputIsRecent() || shownSince === null) return;
		const waited = env.now() - shownSince;
		if (waited < DWELL_MS) {
			dwellTimer = env.setTimer(tryClaim, DWELL_MS - waited);
			return;
		}
		claimed = true;
		focused = null;
		publishFocus();
		publishDocumentOpenCapability();
		armIdle();
	}

	function release(extra: Record<string, unknown>) {
		clear(dwellTimer);
		clear(idleTimer);
		dwellTimer = idleTimer = null;
		if (claimed && target) env.send(frame({ state: 'background', ...extra }));
		claimed = false;
		focused = null;
	}

	return {
		show(threadId, surface = 'chat') {
			if (target && target.threadId === threadId && target.surface === surface) return;
			release({});
			documentOpen = null;
			target = { threadId, surface };
			shownSince = env.isVisible() ? env.now() : null;
			tryClaim();
		},
		hide() {
			release({});
			documentOpen = null;
			target = null;
			shownSince = null;
		},
		visibilityChanged() {
			if (!env.isVisible()) {
				release({ reason: 'hidden' });
				shownSince = null;
				return;
			}
			if (shownSince === null) shownSince = env.now();
			if (dwellTimer === null) tryClaim();
		},
		input() {
			if (claimed) {
				publishFocus();
				return armIdle();
			}
			if (dwellTimer === null) tryClaim();
		},
		focusChanged() {
			publishFocus();
		},
		reconnected() {
			if (!claimed || !target) return;
			focused = null;
			publishFocus();
			publishDocumentOpenCapability();
		},
		updateDocumentOpenCapability(capability) {
			if (documentOpen?.canShow === capability.canShow
				&& documentOpen.showing === capability.showing) return;
			documentOpen = capability;
			publishDocumentOpenCapability();
		},
		get documentOpenCapability() {
			return documentOpen;
		},
		get displayedThreadId() {
			return target?.threadId ?? null;
		},
		get claimed() {
			return claimed;
		},
		get claimedThreadId() {
			return claimed && target ? target.threadId : null;
		},
	};
}

let browserTracker: PresenceTracker | null = null;

function browserEnv(): PresenceEnv {
	return {
		send(frameToSend) {
			if (ws && ws.readyState === WebSocket.OPEN) ws.send(JSON.stringify(frameToSend));
		},
		now: () => Date.now(),
		lastInputAt: lastUserInputAt,
		isVisible: () => document.visibilityState === 'visible',
		isFocused: () => document.visibilityState === 'visible' && (
			document.hasFocus()
			|| document.activeElement instanceof HTMLIFrameElement
			|| /Android.*Mobile|iPhone|iPod/i.test(navigator.userAgent)
		),
		setTimer: (fn, ms) => setTimeout(fn, ms),
		clearTimer: (handle) => clearTimeout(handle as ReturnType<typeof setTimeout>),
	};
}

function ensureBrowserTracker(): PresenceTracker | null {
	if (browserTracker) return browserTracker;
	if (typeof document === 'undefined' || typeof window === 'undefined') return null;
	const tracker = createPresenceTracker(browserEnv());
	document.addEventListener('visibilitychange', () => tracker.visibilityChanged());
	window.addEventListener('focus', () => tracker.focusChanged());
	window.addEventListener('blur', () => setTimeout(() => tracker.focusChanged(), 0));
	onUserInput(() => tracker.input());
	connectionState.subscribe((stateNow) => {
		if (stateNow === 'connected') tracker.reconnected();
	});
	browserTracker = tracker;
	return tracker;
}

/** Start reporting presence for `threadId` while this view displays it. */
export function startPresence(threadId: string, surface: PresenceSurface = 'chat'): void {
	const tracker = ensureBrowserTracker();
	tracker?.show(threadId, surface);
	const capability = get(displayedDocument);
	if (capability?.threadId === threadId) tracker?.updateDocumentOpenCapability(capability.value);
}

/** Stop reporting presence: the view no longer displays a thread. */
export function stopPresence(): void {
	browserTracker?.hide();
}

/**
 * The one holder of the document the canvas shows: the thread a view displays and what its canvas
 * puts on screen. The published frame and the viewing checks both read this, so they cannot disagree.
 */
export const displayedDocument = writable<{ threadId: string; value: DocumentOpenCapability } | null>(null);

/** Update one chat view's document delivery facts only when they change. */
export function updateDocumentOpenCapability(
	threadId: string | null,
	capability: DocumentOpenCapability,
): void {
	if (!threadId) {
		displayedDocument.set(null);
		return;
	}
	const current = get(displayedDocument);
	if (current?.threadId === threadId && current.value.canShow === capability.canShow
		&& current.value.showing === capability.showing) return;
	displayedDocument.set({ threadId, value: capability });
	if (browserTracker?.displayedThreadId === threadId) browserTracker.updateDocumentOpenCapability(capability);
}

/** A view that stops displaying `threadId` forgets it, unless another view has published since. */
export function releaseDisplayedDocument(threadId: string | null): void {
	if (threadId && get(displayedDocument)?.threadId === threadId) displayedDocument.set(null);
}

/** The current browser tracker, without creating one for an incoming frame. */
export function getPresenceTracker(): PresenceTracker | null {
	return browserTracker;
}
