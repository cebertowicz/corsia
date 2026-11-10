import { onDestroy } from 'svelte';
import { paneCollapsed } from '$lib/stores/paneCollapsed';

/** The reader's place in a comment field: where to find that field again in whichever form the surface takes, and the caret they left. */
type WritingPlace = { where: string; caret: [number, number] | null };

/**
 * How long a place is re-placed after the panel closes: as long as the shell is still easing the width back and the
 * form the list lands in is still settling.
 */
const PLACE_WINDOW_MS = 1500;

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
	if (field && card) return { where: `[data-thread-id="${CSS.escape(card.getAttribute('data-thread-id') ?? '')}"] .ct-input`, caret: caretOf(field) };
	return field?.matches('.composer [data-drawer-autofocus]') ? { where: '.composer [data-drawer-autofocus]', caret: caretOf(field) } : null;
}

/**
 * Hands the reply or comment field the reader was writing in back across one trip of the Workspace panel, with the
 * draft and the caret. Opening the panel takes width from the document and the comment list is rebuilt in its other
 * form — the column beside the text, or the sheet over it — and even where the form is kept, focus has gone out to
 * the panel's own control to open it and nothing brings it back.
 *
 * The place is only carried when focus left the field for the panel's own control: focus anywhere else is the reader
 * moving on, and nothing is held for it. While the panel is open nothing is pulled back into the document.
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
		const field = area.querySelector<HTMLElement>(pending.where);
		if (!field) return;
		if (field !== document.activeElement) field.focus({ preventScroll: true });
		// A field in a form the surface has not drawn yet takes no focus.
		if (document.activeElement !== field || placed.has(field)) return;
		// Once per field: a form may have focused it already, with the caret at the end.
		placed.add(field);
		if (pending.caret && field instanceof HTMLTextAreaElement) field.setSelectionRange(...pending.caret);
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

	/** Takes the place being carried, if one is, and hands writing back to it. */
	function carryBack() {
		if (!carried) return;
		pending = carried;
		carried = null;
		keepPlacedWhileShellMoves();
	}

	// The surface's element changes with every form the list takes, and the one being replaced can clear the caller's
	// reference to it on its way out, so focus is watched on the document and filtered to the surface's own fields.
	$effect(() => {
		// The place focus last left, to be carried if it goes on to the panel's own control: from a reply to the toggle by
		// keyboard the way crosses the card's own buttons, which are not the reader moving on.
		let left: WritingPlace | null = null;
		const onFocusIn = (event: FocusEvent) => {
			const place = placeOf(event.target);
			const target = event.target instanceof Element ? event.target : null;
			if (!place) {
				if (toPanelControl(target)) { carried ??= left; return; }
				// Another field, or the document's text, is the reader moving on: nothing is carried and nothing is placed.
				if (target?.closest('input, textarea, select, [contenteditable="true"], .cm-content')) { left = null; carried = null; pending = null; stopTrying(); }
				return;
			}
			left = null;
			// They chose a place to write that is not the one being carried: that one is theirs.
			if (pending && place.where !== pending.where) { carried = null; pending = null; stopTrying(); }
		};
		const onFocusOut = (event: FocusEvent) => {
			const place = placeOf(event.target);
			if (place) left = place;
		};
		document.addEventListener('focusin', onFocusIn);
		document.addEventListener('focusout', onFocusOut);
		return () => {
			document.removeEventListener('focusin', onFocusIn);
			document.removeEventListener('focusout', onFocusOut);
		};
	});

	// Watched on the store itself, not through an effect: the panel's move rebuilds the surface around the writing,
	// and the hand-back must not depend on which component instance happens to be alive or when it re-renders.
	const stopWatchingPanel = paneCollapsed.subscribe((nowClosed) => {
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
