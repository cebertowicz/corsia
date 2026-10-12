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
	const border = () => {
		const style = getComputedStyle(node);
		return style.boxSizing === 'border-box' ? 0 : (parseFloat(style.borderTopWidth) || 0) + (parseFloat(style.borderBottomWidth) || 0);
	};
	/** Content that animates its own height already carries the panel with it, frame by frame. */
	const contentEases = () => typeof node.getAnimations === 'function' && node.getAnimations({ subtree: true }).some((item) =>
		item !== animation && item.playState === 'running' && item.effect?.getKeyframes().some((frame) => 'height' in frame));
	const fadeIn = () => {
		const style = getComputedStyle(node);
		const duration = Number.parseFloat(style.getPropertyValue('--dur-tight')) || 120;
		for (const child of Array.from(node.children)) {
			if (child instanceof HTMLElement && !child.classList.contains('drawer-chrome')) child.animate([{ opacity: 0 }, { opacity: 1 }], { duration, easing: style.getPropertyValue('--ease-snap') || 'ease-out' });
		}
	};
	const run = (from: number, to: number) => {
		const inset = border();
		animation = node.animate(
			[{ height: `${from - inset}px` }, { height: `${to - inset}px` }],
			{ duration: 200, easing: 'cubic-bezier(0.22, 1, 0.36, 1)' },
		);
		const done = () => {
			animation = null;
			// Content may have changed again while this ran; ease on from where it stopped.
			const now = node.getBoundingClientRect().height;
			last = now;
			if (enabled && Math.abs(now - to) > 1) run(to, now);
		};
		animation.addEventListener('finish', done, { once: true });
		animation.addEventListener('cancel', () => { animation = null; last = node.getBoundingClientRect().height; }, { once: true });
	};
	const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => {
		const now = node.getBoundingClientRect().height;
		if (animation || detentMoves > 0) return;
		const from = last;
		last = now;
		if (!enabled || typeof node.animate !== 'function' || Math.abs(now - from) <= 1) return;
		if (reduced) fadeIn();
		else if (!contentEases()) run(from, now);
	});
	const moveStart = (event: TransitionEvent) => { if (event.target === node && event.propertyName === 'max-height') detentMoves++; };
	const moveEnd = (event: TransitionEvent) => {
		if (event.target !== node || event.propertyName !== 'max-height') return;
		detentMoves = Math.max(0, detentMoves - 1);
		if (detentMoves === 0) last = node.getBoundingClientRect().height;
	};
	node.addEventListener('transitionrun', moveStart);
	node.addEventListener('transitionend', moveEnd);
	node.addEventListener('transitioncancel', moveEnd);
	observer?.observe(node);
	return {
		update(next: { enabled: boolean; reduced?: boolean }) { enabled = next.enabled; reduced = next.reduced ?? false; },
		destroy() {
			observer?.disconnect();
			animation?.cancel();
			node.removeEventListener('transitionrun', moveStart);
			node.removeEventListener('transitionend', moveEnd);
			node.removeEventListener('transitioncancel', moveEnd);
		},
	};
}
