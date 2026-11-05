import { carryPanelHeight } from '$lib/actions/settleHeight';

/** `from` and `liveFrom` are the opacities the old copy and the live layout start at: 1 and 0, or where an interrupted fade had reached. */
type Capture = { overlay: HTMLElement; height: number; scroll: { node: HTMLElement; top: number; left: number }[]; from?: number; liveFrom?: number };

/** The animations holding an element's height: a transition still easing it, or finished and not yet retired by its own frame loop. */
const heightHolds = (node: HTMLElement) => (typeof node.getAnimations === 'function' ? node.getAnimations() : []).filter((item) => (item.effect as KeyframeEffect | null)?.getKeyframes?.().some((frame) => 'height' in frame));

/** The height the content wants, read from the children, which stay laid out at full size while a held animation clips the node. */
function contentHeight(node: HTMLElement): number {
	const top = node.getBoundingClientRect().top;
	const style = getComputedStyle(node);
	const bottom = Math.max(0, ...Array.from(node.children, (child) => child.getBoundingClientRect().bottom - top));
	return bottom + (parseFloat(style.paddingBottom) || 0) + (parseFloat(style.borderBottomWidth) || 0);
}

const shown = (node: HTMLElement) => { const value = Number.parseFloat(getComputedStyle(node).opacity); return Number.isFinite(value) ? value : 1; };

export type MentionPlayback = { cancel: () => void; handoff: () => Capture | null; supersede: () => {from: number; liveFrom: number; midFade: boolean} };

export function mentionHost(input: HTMLElement): HTMLElement | null {
	return input.closest<HTMLElement>('.drawer-panel') ?? input.closest<HTMLElement>('[data-popover]') ?? input.closest<HTMLElement>('.ct');
}

/** A non-interactive copy of the old layout; the live textarea is never moved or replaced. */
export function captureMentionLayout(region: HTMLElement | null, input: HTMLTextAreaElement, reduced: boolean): Capture | null {
	return captureLayout(reduced ? mentionHost(input) : region);
}

export function captureLayout(source: HTMLElement | null): Capture | null {
	if (!source) return null;
	const rect = source.getBoundingClientRect();
	// A region at the first frame of its own intro is already as wide as it will be, and no tall yet: the change still has to ease from there.
	if (!rect.width || (!rect.height && !source.isConnected)) return null;
	const copy = source.cloneNode(true) as HTMLElement;
	const sourceNodes = [source, ...source.querySelectorAll<HTMLElement>('*')];
	const copyNodes = [copy, ...copy.querySelectorAll<HTMLElement>('*')];
	const scroll: Capture['scroll'] = [];
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
	return { overlay: copy, height: rect.height, scroll };
}

export function playLayoutCrossfade(capture: Capture, live: HTMLElement | null): MentionPlayback {
	return playMentionLayout(capture, live, true);
}

export function playMentionLayout(capture: Capture, live: HTMLElement | null, reduced: boolean, carriable = true): MentionPlayback {
	const { overlay, height, scroll, from = 1, liveFrom = 0 } = capture;
	const style = getComputedStyle(live ?? document.documentElement);
	const duration = Number.parseFloat(style.getPropertyValue('--dur-tight')) || 120;
	const easing = style.getPropertyValue('--ease-snap').trim() || 'ease-out';
	document.body.append(overlay);
	scroll.forEach(({ node, top, left }) => { node.scrollTop = top; node.scrollLeft = left; });
	const oldFade = overlay.animate([{ opacity: from }, { opacity: 0 }], { duration, easing });
	const animations: Animation[] = [oldFade];
	let syncFrame = 0;
	// Read before this layer adds its own animations. A transition still holding the height (the picker's intro, when the answer lands during it
	// or in the frames before its frame loop retires it) makes the region measure as its own keyframes, not as the new content.
	const held = live && !reduced ? heightHolds(live) : [];
	if (live) {
		animations.push(live.animate([{ opacity: liveFrom }, { opacity: 1 }], { duration, easing }));
		if (!reduced) {
			const current = held.length ? contentHeight(live) : live.getBoundingClientRect().height;
			const inset = style.boxSizing === 'border-box' ? 0 : (parseFloat(style.paddingTop) || 0) + (parseFloat(style.paddingBottom) || 0) + (parseFloat(style.borderTopWidth) || 0) + (parseFloat(style.borderBottomWidth) || 0);
			if (Math.abs(current - height) > 1) {
				overlay.style.overflow = 'hidden';
				// Ended through the platform, so its own finish handler still runs and retires it: it cannot hold the height once this ease is done.
				held.forEach((item) => item.finish());
				const ease = live.animate([
					{ height: `${Math.max(0, height - inset)}px`, overflow: 'hidden' },
					{ height: `${Math.max(0, current - inset)}px`, overflow: 'hidden' },
				], { duration, easing });
				animations.push(ease);
				carryPanelHeight(live, ease.finished);
				// The live clip's top and height move every frame as the panel resizes (and a host can scroll), so the copy follows it.
				const syncClip = () => {
					if (!live.isConnected || !overlay.isConnected) return;
					const rect = live.getBoundingClientRect();
					Object.assign(overlay.style, { top: `${rect.top}px`, left: `${rect.left}px`, height: `${rect.height}px` });
					syncFrame = requestAnimationFrame(syncClip);
				};
				syncClip();
			}
		}
	}
	let handedOff = false;
	let done = false;
	oldFade.finished.catch(() => {}).finally(() => { done = true; if (handedOff) return; cancelAnimationFrame(syncFrame); overlay.remove(); });
	return {
		cancel: () => { cancelAnimationFrame(syncFrame); animations.forEach((animation) => animation.cancel()); overlay.remove(); },
		// A change that lands mid-fade carries on from what is on screen (the same copy, the same height, the opacities reached)
		// instead of from the settled layout, which would skip what the person was just shown. A fade that has finished has nothing to hand on.
		// `carriable` is false for the copy made to open the picker: that picture is the layout from before the person saw any of it,
		// so the next change must capture the layout actually on screen rather than carry the open back out.
		handoff: () => {
			if (!carriable || done || oldFade.playState === 'finished') return null;
			const next: Capture = { overlay, height: live?.getBoundingClientRect().height ?? height, scroll: [], from: shown(overlay), liveFrom: live ? shown(live) : 0 };
			handedOff = true;
			cancelAnimationFrame(syncFrame);
			animations.forEach((animation) => animation.cancel());
			return next;
		},
		// A change that declines the handoff (the opening copy, whose picture the person was never shown) still has to
		// be stopped: otherwise its copy keeps fading underneath the new one, the host keeps the old opacity animation,
		// and the new copy starts at full opacity over a screen that was mid-fade. Returns the opacities that were on
		// screen so the new fade starts there; `midFade` is false once this playback's own fade is over, and then the
		// next change crossfades from the settled layout as usual.
		supersede: () => {
			const midFade = overlay.isConnected && oldFade.playState === 'running';
			const seen = {from: midFade ? shown(overlay) : 1, liveFrom: midFade && live ? shown(live) : 0, midFade};
			cancelAnimationFrame(syncFrame);
			animations.forEach((animation) => animation.cancel());
			overlay.remove();
			return seen;
		},
	};
}
