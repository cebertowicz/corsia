import { tick } from 'svelte';
import { EditorView } from '@codemirror/view';
import type { OpenImageRequest } from '$lib/cm/markdown-image';
import { applyPlace, captureDocumentPlace, heldPlace, holdPlace, type TextDocumentPlace } from '$lib/documentReadingPlace';

export type { TextDocumentPlace };

export interface RestoreDocumentPlaceOptions {
	/** A tab return lays its saved place before the tab shows; an image Back holds it while the pane width eases. */
	mode?: 'back' | 'tab-return';
	/** Called once the saved place is laid, while the restore keeps holding it. */
	onPlaced?: () => void;
	/** A newer restore superseded this one; it must stop before touching the document again. */
	signal?: AbortSignal;
}

const nextFrame = () => new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));

/** Frames the pane width must hold before it counts as settled. */
const STABLE_FRAMES = 3;
/** The editor re-measures the text it left unrendered some time after the width returns, so the place is laid each frame for this long (about 0.75 s) after it last moved. */
const HOLD_FRAMES = 45;
/** The pane eases for under half a second; the restore gives up after about four. */
const MAX_FRAMES = 240;

/**
 * The place a restore holds. An image is held by its line, unless an edit has moved its markdown: then the line that
 * was at the top at the saved offset stands in.
 */
function restoredPlace(view: EditorView, place: OpenImageRequest | TextDocumentPlace): TextDocumentPlace {
	if (!('src' in place)) return place;
	const moved = place.to > view.state.doc.length || !view.state.sliceDoc(place.from, place.to).startsWith('![');
	return moved ? captureDocumentPlace(view, place.scrollTop) : { from: place.from, top: place.top, scrollTop: place.scrollTop };
}

/**
 * Puts a document back where it was after an image viewer closes or a tab returns: makes the saved place the one the
 * editor holds, and lays it on every frame while the pane eases back to its width and the editor measures the text it
 * had not drawn. The editor's resize observation lays it again after any layout inside a frame, so no painted frame shows
 * the document out of place. The restore ends when the width and the place have held, or when the reader scrolls or a
 * newer restore takes over, either of which gives the editor another place.
 */
export async function restoreDocumentPlace(
	root: HTMLElement | undefined,
	place: OpenImageRequest | TextDocumentPlace,
	options: RestoreDocumentPlaceOptions = {},
): Promise<void> {
	await tick();
	if (options.signal?.aborted) return;
	const scroller = root?.querySelector<HTMLElement>('.cm-scroller');
	const view = scroller && EditorView.findFromDOM(scroller);
	if (!root || !view) return;
	const target = holdPlace(view, restoredPlace(view, place));
	if (options.mode === 'tab-return') options.onPlaced?.();
	let width = root.getBoundingClientRect().width;
	let steady = 0;
	let held = 0;
	for (let frame = 0; frame < MAX_FRAMES && (held < HOLD_FRAMES || steady < STABLE_FRAMES); frame++) {
		await nextFrame();
		if (options.signal?.aborted || heldPlace(view) !== target || !view.dom.isConnected) return;
		const now = root.getBoundingClientRect().width;
		steady = now === width ? steady + 1 : 0;
		width = now;
		held = !applyPlace(view) && steady > 0 ? held + 1 : 0;
	}
}
