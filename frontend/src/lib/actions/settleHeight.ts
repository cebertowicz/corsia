/**
 * Eases a bottom-anchored panel from its old height to its new one when its content changes the height
 * (a filter that leaves fewer rows, a footer appearing), instead of snapping in a single frame.
 *
 * Only content-driven changes are eased: a detent move already transitions `max-height`, and a drag is the
 * user's own motion, so both are left alone. With `enabled` false (reduced motion, a drag) the change is a cut.
 */
export function settleHeight(node: HTMLElement, options: { enabled: boolean }) {
	let enabled = options.enabled;
	let last = node.getBoundingClientRect().height;
	let animation: Animation | null = null;
	let detentMoves = 0;
	const border = () => {
		const style = getComputedStyle(node);
		return style.boxSizing === 'border-box' ? 0 : (parseFloat(style.borderTopWidth) || 0) + (parseFloat(style.borderBottomWidth) || 0);
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
		if (enabled && typeof node.animate === 'function' && Math.abs(now - from) > 1) run(from, now);
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
		update(next: { enabled: boolean }) { enabled = next.enabled; },
		destroy() {
			observer?.disconnect();
			animation?.cancel();
			node.removeEventListener('transitionrun', moveStart);
			node.removeEventListener('transitionend', moveEnd);
			node.removeEventListener('transitioncancel', moveEnd);
		},
	};
}
