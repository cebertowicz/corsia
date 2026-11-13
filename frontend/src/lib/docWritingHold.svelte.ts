import { onDestroy } from 'svelte';
import { writable } from 'svelte/store';
import { paneCollapsed } from '$lib/stores/paneCollapsed';

/**
 * The reader's place in a comment field: where to find that field again in whichever form the surface takes, the caret
 * they left, and the part of the surface it belongs to (its thread's card, or the new-comment composer).
 */
type WritingPlace = { where: string; caret: [number, number] | null; home: string };

/**
 * How long a place is re-placed after the panel closes: as long as the shell is still easing the width back and the
 * form the list lands in is still settling.
 */
const PLACE_WINDOW_MS = 1500;

/**
 * How long the surface holds the form its writing is in once the panel starts back: short enough that the place is
 * still being re-placed when the form is finally allowed to change, so a card that genuinely changes form is drawn
 * and takes the writing back rather than dropping it.
 */
const FORM_HOLD_MS = 900;

/**
 * True while the panel is easing back and a place is being carried to it. The canvas width passes through values the
 * layout never settles at on its way, and the form the card is in is decided from that width: a card rebuilt in
 * another form mid-move loses the writing, because the field the place is restored to no longer exists.
 */
const held = writable(false);
export const writingHeld = { subscribe: held.subscribe };

/**
 * The place being carried back. One document's comment surface is up at a time, and the panel's move can rebuild it,
 * so the place lives here, outside the component that recorded it, and is honoured when the panel closes — unless the
 * reader wrote somewhere else first, which is what clears it.
 */
let carried: WritingPlace | null = null;

/** The panel's own control: the one thing focus may leave a comment field for without the reader moving on. */
const toPanelControl = (target: EventTarget | null) => target instanceof Element && !!target.closest('[aria-label="Toggle panel"]');

const caretOf = (field: HTMLElement): [number, number] | null => field instanceof HTMLTextAreaElement || field instanceof HTMLInputElement
	? [field.selectionStart ?? 0, field.selectionEnd ?? 0]
	: null;

/** The writing place in a comment surface, in any form it takes (list, margin card, popover, sheet) — a thread's reply or the new-comment field — or null for anything else, or a form on its way out. */
function placeOf(target: EventTarget | null): WritingPlace | null {
	const field = target instanceof HTMLElement && !target.closest('.leaving') ? target : null;
	const card = field?.matches('.ct-input') ? field.closest<HTMLElement>('[data-thread-id]') : null;
	if (field && card) {
		const home = `[data-thread-id="${CSS.escape(card.getAttribute('data-thread-id') ?? '')}"]`;
		return { where: `${home} .ct-input`, caret: caretOf(field), home };
	}
	return field?.matches('.composer [data-drawer-autofocus]') ? { where: '.composer [data-drawer-autofocus]', caret: caretOf(field), home: '.composer' } : null;
}

/** A thread's card or the new-comment composer: the parts of the surface a reader writes in. */
const SURFACE_PART = '[data-thread-id], .composer';

/**
 * Hands the reply or comment field the reader was writing in back across one trip of the Workspace panel, with the
 * draft and the caret. Opening the panel takes width from the document and the comment list is rebuilt in its other
 * form — the column beside the text, or the sheet over it — and even where the form is kept, focus has gone out to
 * the panel's own control to open it and nothing brings it back.
 *
 * The place is carried when the panel moves while focus is in the field (its keyboard shortcut, or a press that does not
 * move focus), or when focus left the field for the panel's own control crossing only that field's own card and the
 * app's controls: focus anywhere else — another field, another thread's card — is the reader moving on, and nothing is
 * held for it. While the panel is open nothing is pulled back into the document.
 *
 * Call it while a comment surface component initialises. `live` is the selector of the form the surface is in now,
 * which is where writing is placed back: the surface's own element cannot answer that, because a form on its way out
 * keeps its element through its outro while the one that replaced it is already in the markup. A column the editor
 * has not found room for beside its text yet takes no focus, so the place is re-placed while the shell is still
 * moving.
 */
export function holdWritingPlace(live: () => string | null) {
	let pending: WritingPlace | null = null, frame = 0;
	const placed = new WeakSet<HTMLElement>();

	const stopTrying = () => {
		if (frame) cancelAnimationFrame(frame);
		frame = 0;
	};

	/** The surface as it stands now: whichever form the caller says is up, and nothing when neither is. */
	const liveSurface = () => {
		const selector = live();
		return selector ? document.querySelector<HTMLElement>(selector) ?? undefined : undefined;
	};

	/** Focuses the carried place in this surface, if it holds it and takes focus. */
	function tryPlace(area: HTMLElement | undefined) {
		if (!pending || !area?.isConnected) return;
		// While the shell is moving, two forms of the same card can be in the document at once — the one on its way out
		// keeps its element through its outro — and only the form the surface has drawn takes focus. So every copy is
		// offered the place, and the first that accepts it keeps it.
		for (const field of area.querySelectorAll<HTMLElement>(pending.where)) {
			if (field !== document.activeElement) field.focus({ preventScroll: true });
			if (document.activeElement !== field) continue;
			// Once per field: a form may have focused it already, with the caret at the end.
			if (placed.has(field)) return;
			placed.add(field);
			if (pending.caret && field instanceof HTMLTextAreaElement) field.setSelectionRange(...pending.caret);
			return;
		}
	}

	// The list can still be on its way in when the panel closes: the width eases back over a few hundred
	// milliseconds, the column is drawn only once the editor has the room beside its text, and the form being
	// replaced hands focus back to whoever opened it on its way out — after any single restore has already run. So
	// the place is kept while the shell is still moving, and re-placed whenever the list moves out from under it.
	// Placing where writing already is costs nothing, so a surface that has stopped moving is idle.
	function keepPlacedWhileShellMoves() {
		stopTrying();
		const until = Date.now() + PLACE_WINDOW_MS;
		const step = () => {
			frame = 0;
			const area = pending && liveSurface();
			if (!area) { pending = null; return; }
			tryPlace(area);
			if (Date.now() < until) frame = requestAnimationFrame(step);
			else pending = null;
		};
		frame = requestAnimationFrame(step);
	}

	/**
	 * The reader moved on: nothing is carried or placed, and the form is no longer held. The hold is only there for a place
	 * being carried; kept past that, the surface goes on acting on a width the canvas has left (a margin column that is
	 * gone), and the form it should have changed to arrives when the hold lifts as if the reader had opened it.
	 */
	function letGo() {
		carried = null; pending = null; stopTrying();
		held.set(false);
	}

	/** Takes the place being carried, if one is, and hands writing back to it. */
	function carryBack() {
		if (!carried) return;
		pending = carried;
		carried = null;
		held.set(true);
		setTimeout(() => held.set(false), FORM_HOLD_MS);
		keepPlacedWhileShellMoves();
	}

	// The surface's element changes with every form the list takes, and the one being replaced can clear the caller's
	// reference to it on its way out, so focus is watched on the document and filtered to the surface's own fields.
	$effect(() => {
		// The place focus last left, to be carried if it goes on to the panel's own control: from a reply to the toggle by
		// keyboard the way crosses the card's own buttons, which are not the reader moving on.
		let left: WritingPlace | null = null;
		// Set by a press outside the surface and cleared by the next focus: it says the reader moved on. It outlives the
		// field's own focus-out, which lands after the press and would otherwise record the place they pressed away from.
		let pressAway = false;
		const onFocusIn = (event: FocusEvent) => {
			const place = placeOf(event.target);
			const target = event.target instanceof Element ? event.target : null;
			pressAway = false;
			if (!place) {
				if (toPanelControl(target)) { carried ??= left; return; }
				// Another field, or the document's text, is the reader moving on: nothing is carried and nothing is placed.
				if (target?.closest('input, textarea, select, [contenteditable="true"], .cm-content')) { left = null; letGo(); return; }
				// Into another thread's card: the reader went on through the comments, and the place they left is not where
				// they are now. Carrying it would hand focus to a field they only passed through on the way.
				const part = target?.closest(SURFACE_PART);
				if (left && part && !part.matches(left.home)) left = null;
				return;
			}
			left = null;
			// They chose a place to write that is not the one being carried: that one is theirs.
			if (carried && place.where !== carried.where) carried = null;
			if (pending && place.where !== pending.where) { carried = null; pending = null; stopTrying(); }
		};
		const onFocusOut = (event: FocusEvent) => {
			const place = placeOf(event.target);
			if (!place || pressAway) return;
			left = place;
			// Still writing in the place being carried, with the panel open: carry the caret where it is now.
			if (carried?.where === place.where) carried = place;
		};
		// A press outside the comment surface is the reader moving on: another pane, another field, the document. Going to
		// the panel's toggle by keyboard crosses the app's other controls, so only a press says they left, and the toggle
		// itself and anywhere the writing lives keep the place.
		const onPointerDown = (event: PointerEvent) => {
			const target = event.target instanceof Element ? event.target : null;
			// Safari and the desktop app's web view never focus a button that is clicked, so a press on the toggle is where
			// the place is carried: the field still has focus while the press goes down.
			if (toPanelControl(target)) carried ??= placeOf(document.activeElement) ?? left;
			pressAway = !target?.closest('[data-doc-comments-drawer], [data-popover], [aria-label="Toggle panel"]');
			if (!pressAway) return;
			left = null; letGo();
		};
		document.addEventListener('focusin', onFocusIn);
		document.addEventListener('focusout', onFocusOut);
		document.addEventListener('pointerdown', onPointerDown, true);
		return () => {
			document.removeEventListener('focusin', onFocusIn);
			document.removeEventListener('focusout', onFocusOut);
			document.removeEventListener('pointerdown', onPointerDown, true);
		};
	});

	// Watched on the store itself, not through an effect: the panel's move rebuilds the surface around the writing,
	// and the hand-back must not depend on which component instance happens to be alive or when it re-renders.
	// The panel's keyboard shortcut moves it without focus leaving the field: the field the reader is in when it moves is
	// the place. The store answers at once on subscribing, which is no move at all.
	let subscribed = false;
	const stopWatchingPanel = paneCollapsed.subscribe((nowClosed) => {
		const here = subscribed ? placeOf(document.activeElement) : null;
		subscribed = true;
		if (here) carried = here;
		if (nowClosed) carryBack();
		else { pending = null; stopTrying(); }
	});

	onDestroy(() => {
		stopTrying();
		stopWatchingPanel();
		// Rebuilt again before the place landed: leave it for the next instance to pick up.
		if (pending && !carried) carried = pending;
		pending = null;
	});
}
