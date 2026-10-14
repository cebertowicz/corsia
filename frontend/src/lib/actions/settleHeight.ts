/**
 * Eases a bottom-anchored panel from its old height to its new one when its content changes the height
 * (a filter that leaves fewer rows, a footer appearing), instead of snapping in a single frame.
 *
 * Only a change nothing else is easing is eased: a detent move already transitions `max-height`, content that
 * grows through its own height animation (a slide, a textarea) carries the panel with it frame by frame, and a drag
 * is the user's own motion. With `enabled` false (a drag) the change is a cut. With `reduced` the size change cuts
 * and what is new in the panel fades in over `--dur-tight` instead: reduced motion crossfades, nothing slides.
 */
export function settleHeight(node: HTMLElement, options: { enabled: boolean; reduced?: boolean }) {
	let enabled = options.enabled;
	let reduced = options.reduced ?? false;
	let last = node.getBoundingClientRect().height;
	let animation: Animation | null = null;
	let detentMoves = 0;
	let positionedForReveal = false;
	const originalInlinePosition = node.style.position;
	const newContent = new Set<HTMLElement>();
	const insertedContent = new Set<HTMLElement>();
	const opacityRestorers = new Set<() => void>();
	const border = () => {
		const style = getComputedStyle(node);
		return style.boxSizing === 'border-box' ? 0 : (parseFloat(style.borderTopWidth) || 0) + (parseFloat(style.borderBottomWidth) || 0);
	};
	/** Finds motion the content owns, so the panel never competes with it. */
	const hasRunningProperty = (target: Element, property: 'height' | 'opacity') => typeof target.getAnimations === 'function' && target.getAnimations({ subtree: true }).some((item) =>
		item !== animation && item.playState === 'running' && (item.effect as KeyframeEffect | null)?.getKeyframes?.().some((frame) => property in frame));
	/** Content that animates its own height already carries the panel with it, frame by frame. */
	const contentEases = () => hasRunningProperty(node, 'height');
	const contentFades = () => hasRunningProperty(node, 'opacity');
	const insertedContentFades = () => Array.from(insertedContent).some((item) => node.contains(item) && hasRunningProperty(item, 'opacity'));
	const clearNewContent = () => {
		newContent.clear();
		insertedContent.clear();
	};
	const fadeNewContent = () => {
		const style = getComputedStyle(node);
		const additions = Array.from(newContent).filter((item) => node.contains(item) && !Array.from(newContent).some((other) => other !== item && other.contains(item)));
		newContent.clear();
		for (const item of additions) {
			if (hasRunningProperty(item, 'opacity') || typeof item.animate !== 'function') continue;
			const originalOpacity = item.style.getPropertyValue('opacity');
			const originalOpacityPriority = item.style.getPropertyPriority('opacity');
			item.style.setProperty('opacity', '0', originalOpacityPriority);
			const restoreOpacity = () => {
				opacityRestorers.delete(restoreOpacity);
				if (item.style.getPropertyValue('opacity') !== '0' || item.style.getPropertyPriority('opacity') !== originalOpacityPriority) return;
				if (originalOpacity) item.style.setProperty('opacity', originalOpacity, originalOpacityPriority);
				else item.style.removeProperty('opacity');
			};
			opacityRestorers.add(restoreOpacity);
			const contentAnimation = item.animate([{ opacity: 0 }, { opacity: 1 }], {
				duration: Number.parseFloat(style.getPropertyValue('--dur-tight')) || 120,
				easing: style.getPropertyValue('--ease-snap'),
			});
			contentAnimation.addEventListener('finish', restoreOpacity, { once: true });
			contentAnimation.addEventListener('cancel', restoreOpacity, { once: true });
		}
	};
	const fadeRevealedRegion = (from: number, to: number) => {
		if (to <= from + 1) return;
		const style = getComputedStyle(node);
		const reveal = document.createElement('div');
		reveal.dataset.settleHeightReveal = '';
		reveal.setAttribute('aria-hidden', 'true');
		reveal.style.cssText = `position:absolute;top:${Math.max(0, from - border())}px;right:0;bottom:0;left:0;z-index:1;pointer-events:none;background:${style.backgroundColor};`;
		if (style.position === 'static') {
			node.style.position = 'relative';
			positionedForReveal = true;
		}
		node.append(reveal);
		const animation = reveal.animate([{ opacity: 1 }, { opacity: 0 }], {
			duration: Number.parseFloat(style.getPropertyValue('--dur-tight')) || 120,
			easing: style.getPropertyValue('--ease-snap'),
		});
		animation.addEventListener('finish', () => reveal.remove(), { once: true });
		animation.addEventListener('cancel', () => reveal.remove(), { once: true });
	};
	const settleReduced = (from: number, to: number) => {
		if (contentFades()) {
			clearNewContent();
			return;
		}
		fadeNewContent();
		const fadingInsertedContent = insertedContentFades() || opacityRestorers.size > 0;
		insertedContent.clear();
		if (!fadingInsertedContent) fadeRevealedRegion(from, to);
	};
	const run = (from: number, to: number) => {
		const inset = border();
		const style = getComputedStyle(node);
		// `fill: 'both'` holds the end height until `finish`, where the effect is cancelled so content sizes the panel again.
		const current = node.animate(
			[{ height: `${from - inset}px` }, { height: `${to - inset}px` }],
			{
				duration: Number.parseFloat(style.getPropertyValue('--dur-standard')) || 0,
				easing: style.getPropertyValue('--ease-entrance'),
				fill: 'both',
			},
		);
		animation = current;
		const cancelled = () => {
			if (animation !== current) return;
			animation = null;
			last = node.getBoundingClientRect().height;
		};
		const done = () => {
			current.removeEventListener('cancel', cancelled);
			current.cancel();
			if (animation === current) animation = null;
			// Content may have changed again while this ran; ease on from where it stopped.
			const now = node.getBoundingClientRect().height;
			last = now;
			if (enabled && Math.abs(now - to) > 1) run(to, now);
		};
		current.addEventListener('finish', done, { once: true });
		current.addEventListener('cancel', cancelled, { once: true });
	};
	const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
		const now = node.getBoundingClientRect().height;
		if (animation || detentMoves > 0) return;
		const from = last;
		last = now;
		if (!enabled || typeof node.animate !== 'function' || Math.abs(now - from) <= 1) {
			clearNewContent();
			return;
		}
		if (contentEases()) {
			clearNewContent();
			return;
		}
		if (reduced) settleReduced(from, now);
		else {
			clearNewContent();
			run(from, now);
		}
	});
	const moveStart = (event: TransitionEvent) => { if (event.target === node && event.propertyName === 'max-height') detentMoves++; };
	const moveEnd = (event: TransitionEvent) => {
		if (event.target !== node || event.propertyName !== 'max-height') return;
		detentMoves = Math.max(0, detentMoves - 1);
		if (detentMoves !== 0) return;
		const from = last;
		const now = node.getBoundingClientRect().height;
		last = now;
		if (reduced) settleReduced(from, now);
	};
	const additions = typeof MutationObserver === 'undefined' ? null : new MutationObserver((records) => {
		// Only reduced motion fades inserted content; tracking it otherwise would hold nodes nothing consumes.
		if (!reduced || !enabled) return;
		for (const item of Array.from(insertedContent)) if (!node.contains(item)) insertedContent.delete(item);
		let inserted = false;
		for (const record of records) for (const added of record.addedNodes) {
			if (added instanceof HTMLElement && !added.matches('[data-settle-height-reveal]')) {
				newContent.add(added);
				insertedContent.add(added);
				inserted = true;
			}
		}
		if (inserted) fadeNewContent();
	});
	node.addEventListener('transitionrun', moveStart);
	node.addEventListener('transitionend', moveEnd);
	node.addEventListener('transitioncancel', moveEnd);
	observer?.observe(node);
	additions?.observe(node, { childList: true, subtree: true });
	return {
		update(next: { enabled: boolean; reduced?: boolean }) { enabled = next.enabled; reduced = next.reduced ?? false; },
		destroy() {
			observer?.disconnect();
			additions?.disconnect();
			for (const restoreOpacity of Array.from(opacityRestorers)) restoreOpacity();
			if (positionedForReveal && node.style.position === 'relative') node.style.position = originalInlinePosition;
			animation?.cancel();
			node.removeEventListener('transitionrun', moveStart);
			node.removeEventListener('transitionend', moveEnd);
			node.removeEventListener('transitioncancel', moveEnd);
		},
	};
}
