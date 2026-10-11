import { tick } from 'svelte';
import type { OpenDocumentImage } from '$lib/stores/documentImageViewer';

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
 * Puts a document back where it was when one of its images was opened: the scroll position it had, then the image
 * held at the distance below the top it had (where it was, not merely in view: nothing scrolls it into view). The width the document had changes with the workspace pane, so this
 * waits for the layout that follows the viewer closing to settle before it reads sizes, and then follows the image
 * rather than the scroll offset: the editor re-measures the text it left unrendered as the width returns, which
 * moves a saved offset onto different content.
 * The image is found by its markdown range, so an edit that moved it falls back to the scroll position alone.
 */
export async function restoreDocumentPlace(root: HTMLElement | undefined, image: OpenDocumentImage): Promise<void> {
	await tick();
	if (root) await widthSettled(root);
	const scroller = root?.querySelector<HTMLElement>('.cm-scroller');
	if (!root || !scroller) return;
	scroller.scrollTop = image.scrollTop;
	const find = () => [...root.querySelectorAll<HTMLElement>('.cm-md-image')]
		.find((el) => el.dataset.from === String(image.from) && el.dataset.src === image.src);
	let touched = false;
	const onInput = () => { touched = true; };
	for (const type of USER_INPUT) scroller.addEventListener(type, onInput, { capture: true, passive: true });
	try {
		let held = 0;
		for (let frame = 0; frame < MAX_ANCHOR_FRAMES && held < ANCHOR_HOLD_FRAMES && !touched; frame++) {
			await nextFrame();
			if (touched) break;
			const widget = find();
			// The editor draws the image a frame after the scroll position lands, so a missing widget is waited for.
			if (!widget) continue;
			const drift = widget.getBoundingClientRect().top - scroller.getBoundingClientRect().top - image.top;
			if (Math.abs(drift) < ANCHOR_TOLERANCE) held += 1;
			else { held = 0; scroller.scrollTop += drift; }
		}
	} finally {
		for (const type of USER_INPUT) scroller.removeEventListener(type, onInput, { capture: true });
	}
}
