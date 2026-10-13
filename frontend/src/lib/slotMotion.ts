// Motion for a control that arrives in or leaves a row of controls: motion.md's "a row arrives or leaves", turned on
// its side. It grows in where it lands as it fades in; leaving, it fades first, then its space and the row's gap close,
// so the controls beside it slide rather than jump. A Svelte transition run as Web Animations on the motion tokens;
// with reduced motion it only fades. Use it as `in:inlineSlot out:inlineSlot`: `transition:` tells it 'both' either way,
// so it could not know which way to run. `out:inlineSlot={{ instant: true }}` leaves at once, for a control another takes
// the place of, so the two never stand in the row together.
import { get } from 'svelte/store';
import { prefersReducedMotion } from '$lib/stores/viewport';

function token(name: string): string {
	return getComputedStyle(document.documentElement).getPropertyValue(name).trim();
}

const ms = (name: string) => Number.parseFloat(token(name)) || 0;

// Svelte invokes `in:` and `out:` separately when a transition reverses. Keep the
// one browser animation that owns a slot here, so the next direction can replace it
// instead of leaving a completed, forwards-filled exit effect on the same element.
type TrackedAnimation = {
	animation: Animation;
	ownsOverflow: boolean;
	clearsOverflow: boolean;
};

const slotAnimations = new WeakMap<Element, TrackedAnimation>();

type SlotFrame = Record<string, string | number>;

function currentInline(style: CSSStyleDeclaration): SlotFrame {
	return {
		maxWidth: style.maxWidth,
		minWidth: style.minWidth,
		marginInlineStart: style.marginInlineStart,
		opacity: style.opacity,
	};
}

function currentBlock(style: CSSStyleDeclaration): SlotFrame {
	return {
		height: style.height,
		minHeight: style.minHeight,
		paddingTop: style.paddingTop,
		paddingBottom: style.paddingBottom,
		marginTop: style.marginTop,
		marginBottom: style.marginBottom,
		borderTopWidth: style.borderTopWidth,
		borderBottomWidth: style.borderBottomWidth,
		gridTemplateRows: style.gridTemplateRows,
		opacity: style.opacity,
		boxSizing: style.boxSizing,
	};
}

function cancelCurrent(node: HTMLElement, frame: (style: CSSStyleDeclaration) => SlotFrame): SlotFrame | undefined {
	const tracked = slotAnimations.get(node);
	if (!tracked) return undefined;
	const current = frame(getComputedStyle(node));
	if (tracked.ownsOverflow) node.style.removeProperty('overflow');
	tracked.animation.cancel();
	slotAnimations.delete(node);
	return current;
}

function track(node: HTMLElement, animation: Animation, { retainsFill, ownsOverflow, clearsOverflow }: { retainsFill: boolean; ownsOverflow: boolean; clearsOverflow: boolean }): void {
	const tracked = { animation, ownsOverflow, clearsOverflow };
	slotAnimations.set(node, tracked);
	void animation.finished.then(() => {
		if (slotAnimations.get(node) !== tracked) return;
		if (clearsOverflow) node.style.removeProperty('overflow');
		if (!retainsFill) slotAnimations.delete(node);
	}, () => {});
}

function fractionBetween(current: string | number | undefined, start: string | number, target: string | number): number {
	const from = Number.parseFloat(String(current));
	const lower = Number.parseFloat(String(start));
	const upper = Number.parseFloat(String(target));
	const range = Math.abs(upper - lower);
	if (!Number.isFinite(from) || !Number.isFinite(range) || range === 0) return 1;
	return Math.min(1, Math.abs(upper - from) / range);
}

function remainingDuration(full: number, current: SlotFrame | undefined, dimension: string, target: SlotFrame, closed: SlotFrame): number {
	if (!current) return full;
	const distance = Math.max(
		fractionBetween(current[dimension], closed[dimension], target[dimension]),
		fractionBetween(current.opacity, closed.opacity, target.opacity),
	);
	return full * distance;
}

/** The gap, in pixels, of the row the control sits in, past any wrapper that takes no box of its own. */
function rowGap(node: Element): number {
	let parent = node.parentElement;
	while (parent && getComputedStyle(parent).display === 'contents') parent = parent.parentElement;
	return (parent && Number.parseFloat(getComputedStyle(parent).columnGap)) || 0;
}

export function inlineSlot(node: HTMLElement, params: { instant?: boolean } | undefined, { direction }: { direction: 'in' | 'out' | 'both' }) {
	if (direction === 'both') throw new Error('inlineSlot runs one way: use in:inlineSlot out:inlineSlot, not transition:');
	const entering = direction === 'in';
	const current = cancelCurrent(node, currentInline);
	if (!entering && params?.instant) return { duration: 0 };
	const fade = ms('--dur-tight');
	const snap = token('--ease-snap') || 'ease-out';
	if (get(prefersReducedMotion)) {
		const animation = node.animate([{ opacity: current?.opacity ?? (entering ? 0 : 1) }, { opacity: entering ? 1 : 0 }], { duration: remainingDuration(fade, current, 'opacity', { opacity: entering ? 1 : 0 }, { opacity: entering ? 0 : 1 }), easing: snap, fill: entering ? 'none' : 'forwards' });
		track(node, animation, { retainsFill: !entering, ownsOverflow: false, clearsOverflow: false });
		return { duration: remainingDuration(fade, current, 'opacity', { opacity: entering ? 1 : 0 }, { opacity: entering ? 0 : 1 }) };
	}
	// max-width rather than width, so a control that fills the rest of its row (flex: 1) grows too.
	const open = { maxWidth: `${node.getBoundingClientRect().width}px`, minWidth: '0px', marginInlineStart: '0px', opacity: 1 };
	const closed = { maxWidth: '0px', minWidth: '0px', marginInlineStart: `${-rowGap(node)}px`, opacity: 0 };
	const grow = ms('--dur-standard');
	const entrance = token('--ease-entrance') || 'ease-out';
	node.style.overflow = 'hidden';
	if (entering) {
		const duration = remainingDuration(grow, current, 'maxWidth', open, closed);
		const animation = node.animate([current ?? closed, open], { duration, easing: entrance });
		track(node, animation, { retainsFill: false, ownsOverflow: true, clearsOverflow: true });
		return { duration };
	}
	const total = fade + grow;
	const duration = remainingDuration(total, current, 'maxWidth', closed, open);
	const start = current ?? open;
	const fadeOffset = duration ? Math.min(1, fade / duration) : 0;
	const animation = node.animate(
		[{ ...start, easing: snap }, { ...start, opacity: 0, offset: fadeOffset, easing: entrance }, closed],
		{ duration, fill: 'forwards' },
	);
	track(node, animation, { retainsFill: true, ownsOverflow: true, clearsOverflow: false });
	return { duration };
}

// A block that arrives in or leaves a column of blocks (a row of a list): motion.md's "a row arrives or leaves", on its
// own axis. Arriving, its height grows from nothing where it lands as it fades in; leaving, the fade overlaps the
// close so the rows below follow rather than jump. Under reduced motion, only the --dur-tight crossfade remains and
// layout snaps beneath it. `off` plays nothing, for changes the person did not cause (a fold wave and its ghosts own
// those). Use it as `in:blockSlot={…} out:blockSlot={…}`, never `transition:`.
export interface BlockSlotParams { off?: boolean }

/** Collapsed to nothing: no height, spacing or border, so the block takes no room. */
const CLOSED = {
	height: '0px', minHeight: '0px', paddingTop: '0px', paddingBottom: '0px',
	marginTop: '0px', marginBottom: '0px', borderTopWidth: '0px', borderBottomWidth: '0px', gridTemplateRows: '0fr', opacity: 0,
};

export function blockSlot(node: HTMLElement, params: BlockSlotParams | undefined, { direction }: { direction: 'in' | 'out' | 'both' }) {
	if (direction === 'both') throw new Error('blockSlot runs one way: use in:blockSlot out:blockSlot, not transition:');
	const entering = direction === 'in';
	const current = cancelCurrent(node, currentBlock);
	if (params?.off || typeof node.animate !== 'function') return { duration: 0 };
	const fade = ms('--dur-tight');
	const grow = ms('--dur-standard');
	if (!fade || !grow) return { duration: 0 };
	const snap = token('--ease-snap') || 'ease-out';
	const entrance = token('--ease-entrance') || 'ease-out';
	if (get(prefersReducedMotion)) {
		const target = { opacity: entering ? 1 : 0 };
		const rest = { opacity: entering ? 0 : 1 };
		const duration = remainingDuration(fade, current, 'opacity', target, rest);
		const animation = node.animate([{ opacity: current?.opacity ?? rest.opacity }, target], { duration, easing: snap, fill: entering ? 'none' : 'forwards' });
		track(node, animation, { retainsFill: !entering, ownsOverflow: false, clearsOverflow: false });
		return { duration };
	}
	// The block's own box, measured at rest after any prior effect is cancelled, so it grows to exactly the room it will take.
	const open = { height: `${node.getBoundingClientRect().height}px`, gridTemplateRows: '1fr', opacity: 1, boxSizing: 'border-box' };
	const closed = { ...CLOSED, boxSizing: 'border-box' };
	node.style.overflow = 'hidden';
	if (entering) {
		const duration = remainingDuration(grow, current, 'height', open, closed);
		const animation = node.animate([current ?? closed, open], { duration, easing: entrance });
		track(node, animation, { retainsFill: false, ownsOverflow: true, clearsOverflow: true });
		return { duration };
	}
	// motion.md exit timing: calc(var(--dur-standard) * 0.7), with fade and close concurrent.
	const exit = grow * 0.7;
	const duration = remainingDuration(exit, current, 'height', closed, open);
	const animation = node.animate(
		[current ?? open, { ...closed, opacity: 0 }],
		{ duration, easing: snap, fill: 'forwards' },
	);
	track(node, animation, { retainsFill: true, ownsOverflow: true, clearsOverflow: false });
	return { duration };
}
