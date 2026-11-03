import { tick } from 'svelte';
import { EditorView } from '@codemirror/view';
import type { OpenImageRequest } from '$lib/cm/markdown-image';
import { placeTop, restoreOwnsPlace, type TextDocumentPlace } from '$lib/documentReadingPlace';

export type { TextDocumentPlace };

export interface RestoreDocumentPlaceOptions {
	/** A tab return applies its saved place before visibility; an image Back holds it on every frame while the pane width eases. */
	mode?: 'back' | 'tab-return';
	/** Called immediately after the initial offset is set, while canonical anchor correction continues. */
	onPlaced?: () => void;
	/** A newer restore superseded this one; it must stop before touching the document again. */
	signal?: AbortSignal;
}

const nextFrame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));

/** Frames the pane width must hold before it counts as settled. */
const STABLE_FRAMES = 3;
/** The editor re-measures the text it left unrendered some time after the width returns, so the image is held for this long (about 0.75 s) after it last moved. */
const ANCHOR_HOLD_FRAMES = 45;
/** The pane eases for under half a second; the hold gives up after about four. */
const MAX_ANCHOR_FRAMES = 240;
/** Anything the user does to the document ends the hold; it never fights their hand. */
const USER_INPUT = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const;
/** Closer than this, in pixels, the image is where it was. */
const ANCHOR_TOLERANCE = 1;
/**
 * Moves a returning tab makes inside one measure pass: each drawn stretch of text corrects the height estimate of the
 * text above it, so the line settles over three or so moves. The editor restarts its pass at most five times per frame.
 */
const MEASURED_MOVES = 4;

/** Where the anchor is now, in pixels below the scroller's top; null when it cannot be found. */
type AnchorTop = () => number | null;

/** What a measured placement did: the anchor was not found, already in place, or moved there. */
type Placed = 'missing' | 'held' | 'moved';

/**
 * Lays the anchor at its saved distance inside the editor's own measure pass, so the frame painted next already shows it
 * in place. The offset alone is not enough: the editor estimates the height of text it has not drawn, so the saved offset
 * lands on a different line until it measures. Each move is made from what the editor has just measured, never from a
 * freshly mounted editor's default estimates, and is measured again within the same pass.
 *
 * `now` runs the pass at once, inside the current frame, instead of in the editor's next one: an animation-frame
 * callback otherwise paints the layout it read before the editor measures the width that frame gave it.
 */
function placeInMeasure(view: EditorView, scroller: HTMLElement, anchorTop: AnchorTop, top: number, now = false, signal?: AbortSignal): Promise<Placed> {
	return new Promise((resolve) => {
		let moves = 0;
		const measure = () => view.requestMeasure({
			read: anchorTop,
			write: (current) => {
				// A newer restore owns the document now; this one stops moving it.
				if (current === null || signal?.aborted) {
					resolve(moves ? 'moved' : 'missing');
					return;
				}
				const drift = current - top;
				if (Math.abs(drift) < ANCHOR_TOLERANCE || moves >= MEASURED_MOVES) {
					resolve(moves ? 'moved' : 'held');
					return;
				}
				moves += 1;
				scroller.scrollTop += drift;
				// The editor draws the text scrolled to and runs this again before the frame is painted.
				measure();
			},
		});
		measure();
		// Reading the layout through the editor runs its pending measure pass now (CodeMirror's readMeasured).
		if (now) view.lineBlockAt(0);
		// An editor torn down before it measures never answers; the document still shows.
		void nextFrame().then(nextFrame).then(() => resolve('missing'));
	});
}

/**
 * Reads where the anchor sits inside a measure pass. The image is its drawn widget; text is its line in the editor's
 * height map, the lookup its place was captured with. An image the editor has not drawn stands in by the height it
 * estimates from the drawn text; an image whose markdown is among the drawn text but has no widget there has moved,
 * and is not found.
 */
function anchorReader(root: HTMLElement, scroller: HTMLElement, view: EditorView, place: OpenImageRequest | TextDocumentPlace): AnchorTop {
	const from = Math.min(place.from, view.state.doc.length);
	const image = 'src' in place ? place : null;
	return () => {
		if (image) {
			const widget = [...root.querySelectorAll<HTMLElement>('.cm-md-image')]
				.find((el) => el.dataset.from === String(image.from) && el.dataset.src === image.src);
			if (widget) return widget.getBoundingClientRect().top - scroller.getBoundingClientRect().top;
			if (from >= view.viewport.from && from <= view.viewport.to) return null;
		}
		return placeTop(view, from);
	};
}

/** Where the drawn image widget is, read from the page alone: without an editor to measure through, or after a layout the editor has not measured yet. Text has no anchor then. */
function widgetReader(root: HTMLElement, scroller: HTMLElement, place: OpenImageRequest | TextDocumentPlace): AnchorTop {
	const image = 'src' in place ? place : null;
	return () => {
		const widget = image && [...root.querySelectorAll<HTMLElement>('.cm-md-image')]
			.find((el) => el.dataset.from === String(image.from) && el.dataset.src === image.src);
		return widget ? widget.getBoundingClientRect().top - scroller.getBoundingClientRect().top : null;
	};
}

/**
 * Puts a document back where it was after an image viewer closes or a tab returns, by holding either the image or the
 * first visible text line at its saved viewport distance. A tab return lays it before the tab shows. Back holds it from
 * the first frame the document shows, on every frame while the pane eases back to its width, and then while CodeMirror
 * re-measures unrendered text: each frame's correction runs inside the editor's measure pass for that frame, and a
 * rewrap the pane's width causes later in the frame is corrected before it paints, so no painted frame shows the
 * document out of place. An image whose markdown moved falls back to the offset alone.
 */
export async function restoreDocumentPlace(
	root: HTMLElement | undefined,
	place: OpenImageRequest | TextDocumentPlace,
	options: RestoreDocumentPlaceOptions = {},
): Promise<void> {
	await tick();
	if (options.signal?.aborted) return;
	const scroller = root?.querySelector<HTMLElement>('.cm-scroller');
	if (!root || !scroller) return;
	const view = EditorView.findFromDOM(scroller);
	const anchorTop = view ? anchorReader(root, scroller, view, place) : widgetReader(root, scroller, place);
	const back = options.mode !== 'tab-return';
	let touched = false;
	const onInput = () => { touched = true; };
	// A narrowing pane rewraps the text after the animation frame has placed it. The editor's resize observation runs after
	// that layout and before its paint, so the drawn image is moved there by the layout the frame will show. The editor's
	// own heights are a frame behind then, so only a drawn widget is read; the next frame's pass measures the rest. From
	// here until it ends, the restore owns the editor's offset: a header that moves the editor meanwhile is answered by
	// laying the place against the scroller's top, never corrected a second time.
	let resized = false;
	const drawnTop = widgetReader(root, scroller, place);
	const release = view && restoreOwnsPlace(view, {
		layout: () => {
			if (touched || options.signal?.aborted) return;
			resized = true;
			const current = drawnTop();
			if (current !== null && Math.abs(current - place.top) >= ANCHOR_TOLERANCE) scroller.scrollTop += current - place.top;
		},
	});
	try {
		if (!back) {
			if (view && !('src' in place)) {
				// The editor holds the text that was at the top when its measure pass began: an offset written inside the pass is
				// pushed back by that text's corrected height. Laying the saved line at its place by the editor's estimated heights
				// before the pass makes it the line the editor holds; the measured placement then corrects the estimate. The
				// editor's own scroll target would lay the line's first character instead, which sits below the line's top, so it
				// lands the line a few pixels off the place the measured placement holds and the two undo each other.
				scroller.scrollTop += placeTop(view, Math.min(place.from, view.state.doc.length)) - place.top;
				await placeInMeasure(view, scroller, anchorTop, place.top, true, options.signal);
			} else scroller.scrollTop = place.scrollTop;
			if (options.signal?.aborted) return;
			options.onPlaced?.();
		} else if (!view) scroller.scrollTop = place.scrollTop;
		for (const type of USER_INPUT) scroller.addEventListener(type, onInput, { capture: true, passive: true });
		let width = root.getBoundingClientRect().width;
		let steady = 0;
		let held = 0;
		for (let frame = 0; frame < MAX_ANCHOR_FRAMES && (held < ANCHOR_HOLD_FRAMES || steady < STABLE_FRAMES) && !touched && !options.signal?.aborted; frame++) {
			await nextFrame();
			if (touched || options.signal?.aborted) break;
			const now = root.getBoundingClientRect().width;
			steady = now === width ? steady + 1 : 0;
			width = now;
			let placed: Placed;
			if (view) {
				placed = await placeInMeasure(view, scroller, anchorTop, place.top, true, options.signal);
				if (placed === 'missing') {
					if (back && frame === 0) scroller.scrollTop = place.scrollTop;
					break;
				}
			} else {
				const currentTop = anchorTop();
				// CodeMirror draws a newly visible line or image a frame after the offset lands.
				if (currentTop === null) continue;
				const drift = currentTop - place.top;
				placed = Math.abs(drift) < ANCHOR_TOLERANCE ? 'held' : 'moved';
				if (placed === 'moved') scroller.scrollTop += drift;
			}
			held = placed === 'held' && steady > 0 && !resized ? held + 1 : 0;
			resized = false;
		}
	} finally {
		release?.();
		for (const type of USER_INPUT) scroller.removeEventListener(type, onInput, { capture: true });
	}
}
