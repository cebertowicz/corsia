import { tick } from 'svelte';
import { EditorView } from '@codemirror/view';
import type { OpenImageRequest } from '$lib/cm/markdown-image';

/** A document position and its viewport distance, stable across CodeMirror re-measurement. */
export interface TextDocumentPlace {
	from: number;
	top: number;
	scrollTop: number;
}

export interface RestoreDocumentPlaceOptions {
	/** A tab return applies its saved place before visibility; an image Back still waits for the pane width. */
	mode?: 'back' | 'tab-return';
	/** Called immediately after the initial offset is set, while canonical anchor correction continues. */
	onPlaced?: () => void;
	/** A newer restore superseded this one; it must stop before touching the document again. */
	signal?: AbortSignal;
}

const nextFrame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));

const STABLE_FRAMES = 3;
const MAX_FRAMES = 90;
/** The editor re-measures the text it left unrendered some time after the width returns, so the image is held for this long (about 0.75 s) after it last moved. */
const ANCHOR_HOLD_FRAMES = 45;
const MAX_ANCHOR_FRAMES = 150;
/** Anything the user does to the document ends the hold; it never fights their hand. */
const USER_INPUT = ['wheel', 'touchstart', 'pointerdown', 'keydown'] as const;
/** Closer than this, in pixels, the image is where it was. */
const ANCHOR_TOLERANCE = 1;
/**
 * Moves a returning tab makes inside one measure pass: each drawn stretch of text corrects the height estimate of the
 * text above it, so the line settles over three or so moves. The editor restarts its pass at most five times per frame.
 */
const MEASURED_MOVES = 4;

/** Resolves once the element's width has held for a few frames: the workspace pane eases back to its width. */
async function widthSettled(el: HTMLElement): Promise<void> {
	let width = el.getBoundingClientRect().width;
	let held = 0;
	for (let frame = 0; frame < MAX_FRAMES && held < STABLE_FRAMES; frame++) {
		await nextFrame();
		const now = el.getBoundingClientRect().width;
		held = now === width ? held + 1 : 0;
		width = now;
	}
}

/**
 * Lays a returning tab's saved line at its saved distance inside the editor's own measure pass, so the frame that
 * reveals the tab is the first one painted and already shows the line in place. The offset alone is not enough: the
 * editor estimates the height of text it has not drawn, so the saved offset lands on a different line until it measures.
 * Each move is made from what the editor has just measured, never from a freshly mounted editor's default estimates,
 * and is measured again within the same pass.
 */
function placeInMeasure(view: EditorView, scroller: HTMLElement, place: TextDocumentPlace): Promise<void> {
	const from = Math.min(place.from, view.state.doc.length);
	return new Promise((resolve) => {
		let moves = 0;
		const measure = () => view.requestMeasure({
			read: () => {
				const coords = view.coordsAtPos(from, 1);
				const box = scroller.getBoundingClientRect();
				// Text the editor has not drawn has no coordinates; its height estimated from the drawn text stands in.
				return coords ? coords.top - box.top : view.documentTop + view.lineBlockAt(from).top - box.top;
			},
			write: (top) => {
				const drift = top - place.top;
				if (Math.abs(drift) < ANCHOR_TOLERANCE || moves++ >= MEASURED_MOVES) {
					resolve();
					return;
				}
				scroller.scrollTop += drift;
				// The editor draws the text scrolled to and runs this again before the frame is painted.
				measure();
			},
		});
		measure();
		// An editor torn down before it measures never answers; the tab still shows.
		void nextFrame().then(nextFrame).then(() => resolve());
	});
}

/** Reads the first visible CodeMirror line as an anchor before its tab leaves the screen. */
export function captureDocumentPlace(root: HTMLElement | undefined): TextDocumentPlace | null {
	const scroller = root?.querySelector<HTMLElement>('.cm-scroller');
	if (!scroller) return null;
	const view = EditorView.findFromDOM(scroller);
	const bounds = scroller.getBoundingClientRect();
	const position = view?.posAtCoords({ x: bounds.left + 1, y: bounds.top + 1 });
	if (!view || position == null) return null;
	const from = view.state.doc.lineAt(position).from;
	const coords = view.coordsAtPos(from, 1);
	if (!coords) return null;
	return { from, top: coords.top - bounds.top, scrollTop: scroller.scrollTop };
}

/**
 * Puts a document back where it was after an image viewer closes or a tab returns. It starts at the saved scroll
 * offset, then holds either the image or the first visible text line at its saved viewport distance while CodeMirror
 * re-measures unrendered text after the layout settles. An image whose markdown moved falls back to the offset alone.
 */
export async function restoreDocumentPlace(
	root: HTMLElement | undefined,
	place: OpenImageRequest | TextDocumentPlace,
	options: RestoreDocumentPlaceOptions = {},
): Promise<void> {
	await tick();
	if (options.signal?.aborted) return;
	if (root && options.mode !== 'tab-return') await widthSettled(root);
	if (options.signal?.aborted) return;
	const scroller = root?.querySelector<HTMLElement>('.cm-scroller');
	if (!root || !scroller) return;
	const editor = options.mode === 'tab-return' && !('src' in place) ? EditorView.findFromDOM(scroller) : null;
	if (editor) await placeInMeasure(editor, scroller, place);
	else scroller.scrollTop = place.scrollTop;
	if (options.signal?.aborted) return;
	options.onPlaced?.();
	const image = 'src' in place ? place : null;
	const view = image ? null : EditorView.findFromDOM(scroller);
	const anchorTop = () => {
		if (image) {
			const widget = [...root.querySelectorAll<HTMLElement>('.cm-md-image')]
				.find((el) => el.dataset.from === String(image.from) && el.dataset.src === image.src);
			return widget ? widget.getBoundingClientRect().top - scroller.getBoundingClientRect().top : null;
		}
		const coords = view?.coordsAtPos(place.from, 1);
		return coords ? coords.top - scroller.getBoundingClientRect().top : null;
	};
	let touched = false;
	const onInput = () => { touched = true; };
	for (const type of USER_INPUT) scroller.addEventListener(type, onInput, { capture: true, passive: true });
	try {
		let held = 0;
		for (let frame = 0; frame < MAX_ANCHOR_FRAMES && held < ANCHOR_HOLD_FRAMES && !touched && !options.signal?.aborted; frame++) {
			await nextFrame();
			if (touched || options.signal?.aborted) break;
			const currentTop = anchorTop();
			// CodeMirror draws a newly visible line or image a frame after the offset lands.
			if (currentTop === null) continue;
			const drift = currentTop - place.top;
			if (Math.abs(drift) < ANCHOR_TOLERANCE) held += 1;
			else { held = 0; scroller.scrollTop += drift; }
		}
	} finally {
		for (const type of USER_INPUT) scroller.removeEventListener(type, onInput, { capture: true });
	}
}
