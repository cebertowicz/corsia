import { getContext, tick, untrack } from 'svelte';
import { SETTLED_LEAF_WIDTH, shellCanvas } from '$lib/stores/artifactCanvas';
import { observeCanvasWidth, type WritingField } from '$lib/docCommentDrawer';
import { focusWhenPlaced } from '$lib/docMarginPlacement';

/**
 * The width a comment surface's form (margin, popover, sheet) follows: the width the canvas settles at, never one a shell
 * transition only passes through. Inside a split it is the leaf's settled width less the leaf's own chrome (measured with
 * the canvas); outside one, the width now. `onWidth` runs at each new width, before the redraw that width can cause.
 * Call while a component initialises.
 */
export function settledCanvasWidth(bounds: () => HTMLElement | null, onWidth: () => void) {
	const settledLeaf = getContext<(() => number | null) | undefined>(SETTLED_LEAF_WIDTH);
	const chrome = (width: number) => {
		const leaf = bounds()?.closest<HTMLElement>('.leaf-col');
		return leaf ? leaf.getBoundingClientRect().width - width : null;
	};
	// Measured as the surface starts, so the first form is the canvas's and a surface that mounts open is not then redrawn.
	const first = bounds()?.getBoundingClientRect().width || Number.POSITIVE_INFINITY;
	let liveWidth = $state(first);
	let leafChrome = $state<number | null>(Number.isFinite(first) ? chrome(first) : null);
	$effect(() => observeCanvasWidth(bounds(), (width) => {
		onWidth();
		liveWidth = width;
		leafChrome = chrome(width);
	}));
	const value = $derived.by(() => { const leaf = settledLeaf?.(); return leaf == null || leafChrome === null ? liveWidth : leaf - leafChrome; });
	return { get value() { return value; } };
}

/**
 * Focus across a form the layout redrew under the same surface (the shell moved, the window resized). That form takes no
 * focus of its own; a reader writing in the old form is handed to the same field in the new one, once, through the new
 * form's own mount focus (`handoff` is that field's selector). Opening a surface is the reader's act and focuses as it
 * always has (`redrawn` is false). Call while a component initialises.
 */
export function createFormHandoff(inputs: {
	surfaceKey: () => string; formKey: () => string; writing: (active: Element | null) => WritingField | null; drawer: () => HTMLElement | undefined;
}) {
	// The field the reader is writing in when a width arrives, read before the redraw it causes can remove that field (the
	// form's block swaps before any effect of the surface runs). Each width overwrites it; the redraw consumes it.
	let noted: WritingField | null = null;
	const note = () => { noted = inputs.writing(document.activeElement); };
	$effect(() => untrack(() => shellCanvas.subscribe(note)));
	let redrawn = $state(false);
	let handoff = $state<string | null>(null);
	/** The outgoing field, kept only for the margin's consume below. */
	let handoffFrom: Element | null = null;
	let lastSurface = '', lastForm = '';
	$effect.pre(() => {
		const key = inputs.surfaceKey(), form = inputs.formKey();
		if (key === lastSurface && form === lastForm) return;
		redrawn = key !== '' && key === lastSurface;
		lastSurface = key; lastForm = form;
		const before = noted;
		noted = null;
		const field = !redrawn ? null : untrack(() => {
			const active = document.activeElement;
			// Focus is gone only when the old form took the field with it: then the field noted at the width is the one.
			return !active || active === document.body || !active.isConnected ? before : inputs.writing(active);
		});
		handoff = field?.selector ?? null;
		handoffFrom = form === 'margin' ? field?.from ?? null : null;
	});
	// The margin column is on the canvas once it mounts: the handed-off field takes focus there, unless the reader has moved on.
	let stopFocus = () => {};
	$effect(() => () => stopFocus());
	$effect(() => {
		const selector = handoff, from = handoffFrom, drawer = inputs.drawer();
		if (!selector || inputs.formKey() !== 'margin' || !drawer) return;
		handoff = null; handoffFrom = null;
		void tick().then(() => {
			const into = drawer.querySelector<HTMLElement>(selector);
			if (!into) return;
			stopFocus();
			stopFocus = focusWhenPlaced(into, from);
		});
	});
	return { note, get redrawn() { return redrawn; }, get handoff() { return handoff; } };
}

