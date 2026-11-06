import { tick } from 'svelte';
import { get } from 'svelte/store';
import { prefersReducedMotion } from '$lib/stores/viewport';
import { clipFade } from '$lib/actions/clipFade';

/** The panel, popover or card whose whole layout a change inside `node` moves. */
export function crossfadeHost(node: HTMLElement): HTMLElement | null {
	return node.closest<HTMLElement>('.drawer-panel') ?? node.closest<HTMLElement>('[data-popover]') ?? node.closest<HTMLElement>('.ct');
}

/** A non-interactive copy of the host as it looks now, laid over it; the live host (and its textarea) is never moved or replaced. */
export function copyLayout(source: HTMLElement): HTMLElement | null {
	const rect = source.getBoundingClientRect();
	if (!rect.width || !rect.height) return null;
	const copy = source.cloneNode(true) as HTMLElement;
	const sourceNodes = [source, ...source.querySelectorAll<HTMLElement>('*')];
	const copyNodes = [copy, ...copy.querySelectorAll<HTMLElement>('*')];
	const scroll: { node: HTMLElement; top: number; left: number }[] = [];
	sourceNodes.forEach((original, index) => {
		const clone = copyNodes[index];
		if (!clone) return;
		// A computed root line-height is in pixels, even when its inherited value
		// was unitless. Preserve each descendant's rendered line-height.
		const rendered = getComputedStyle(original);
		clone.style.lineHeight = rendered.lineHeight;
		if (original === document.activeElement) { clone.style.border = rendered.border; clone.style.boxShadow = rendered.boxShadow; }
		if (original.scrollTop || original.scrollLeft) scroll.push({ node: clone, top: original.scrollTop, left: original.scrollLeft });
	});
	const originals = source.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>('input, textarea');
	const copies = copy.querySelectorAll<HTMLInputElement | HTMLTextAreaElement>('input, textarea');
	originals.forEach((original, index) => { if (copies[index]) copies[index].value = original.value; });
	copy.querySelectorAll('[id]').forEach((child) => child.removeAttribute('id'));
	copy.removeAttribute('id');
	copy.inert = true;
	copy.setAttribute('aria-hidden', 'true');
	const style = getComputedStyle(source);
	// Copy inherited and scoped root styles before portaling the visual copy to the viewport.
	for (let index = 0; index < style.length; index++) {
		const property = style.item(index);
		copy.style.setProperty(property, style.getPropertyValue(property), style.getPropertyPriority(property));
	}
	Object.assign(copy.style, { position: 'fixed', left: `${rect.left}px`, top: `${rect.top}px`, width: `${rect.width}px`, height: `${rect.height}px`, boxSizing: 'border-box', margin: '0', transform: 'none', pointerEvents: 'none', animation: 'none', transition: 'none' });
	let layer: HTMLElement | null = source;
	let z = 0;
	while (layer) { const value = Number.parseInt(getComputedStyle(layer).zIndex, 10); if (Number.isFinite(value)) z = Math.max(z, value); layer = layer.parentElement; }
	copy.style.zIndex = String(z + 1);
	document.body.append(copy);
	scroll.forEach(({ node, top, left }) => { node.scrollTop = top; node.scrollLeft = left; });
	return copy;
}

const shown = (node: HTMLElement) => { const value = Number.parseFloat(getComputedStyle(node).opacity); return Number.isFinite(value) ? value : 1; };

type Fade = { host: HTMLElement; copy: HTMLElement; animations: Animation[] };

/**
 * Under reduced motion, a change to the layout of the host around `anchor` (the tag picker opening, filtering or closing;
 * an Agreed line appearing or going) crossfades: the old layout is copied over the host, the new one is cut in under it,
 * and over `--dur-tight` the copy fades out while the host fades in. Sizes cut; nothing slides.
 *
 * One rule covers a change that lands mid-fade: the copy on screen stays, and the fade starts again from the opacity it has
 * reached, the host always showing the complement. So no frame steps back to a layout or an opacity the screen had left.
 *
 * Runs whenever `key` changes. Returns the transition for the row whose entry or exit makes the change: `clipFade` with
 * normal motion, and a cut under reduced motion, where the crossfade carries it.
 */
export function useLayoutCrossfade(anchor: () => HTMLElement | null | undefined, key: () => unknown) {
	let last: { key: unknown } | null = null;
	let fade: Fade | null = null;
	const stop = () => { fade?.animations.forEach((animation) => animation.cancel()); fade?.copy.remove(); fade = null; };
	const play = (current: Fade) => {
		const { host, copy } = current;
		const from = shown(copy);
		current.animations.forEach((animation) => animation.cancel());
		const style = getComputedStyle(host);
		const options = { duration: Number.parseFloat(style.getPropertyValue('--dur-tight')) || 120, easing: style.getPropertyValue('--ease-snap').trim() || 'ease-out' };
		const out = copy.animate([{ opacity: from }, { opacity: 0 }], options);
		current.animations = [out, host.animate([{ opacity: 1 - from }, { opacity: 1 }], options)];
		// Only the latest fade's finish ends it; a fade it replaced mid-way leaves the copy to it.
		out.addEventListener('finish', () => { if (fade === current && current.animations[0] === out) { copy.remove(); fade = null; } });
	};
	$effect.pre(() => {
		const next = key();
		const node = anchor();
		const changed = last !== null && last.key !== next;
		last = { key: next };
		if (!changed || !node || !get(prefersReducedMotion)) return;
		const host = crossfadeHost(node);
		if (!host) return;
		if (fade?.host !== host) {
			stop();
			const copy = copyLayout(host);
			if (!copy) return;
			fade = { host, copy, animations: [] };
		}
		const current = fade;
		void tick().then(() => { if (fade === current && host.isConnected) play(current); else if (fade === current) stop(); });
	});
	$effect(() => stop);
	return (node: HTMLElement) => (get(prefersReducedMotion) ? { duration: 0 } : clipFade(node));
}
