/**
 * Eases a list row out before its owner removes it: the row's height, padding and rule collapse to nothing while it fades,
 * so the rows below close up instead of jumping, and the panel holding the list (settleHeight) follows frame by frame.
 * Not used with reduced motion: there the owner removes the row at once and the panel's crossfade (settleHeight) carries it.
 * The owner removes the row once `duration` has passed (`leaveDuration`); this action never removes anything itself.
 */
export function leaveDuration(): number {
	const token = (name: string) => Number.parseFloat(getComputedStyle(document.documentElement).getPropertyValue(name)) || 0;
	return token('--dur-standard') || 220;
}

export function leaveRow(node: HTMLElement, options: { leaving: boolean }) {
	let animation: Animation | null = null;
	// The row's size as it last settled, before whatever made it leave also changed its content (a sent reply adds a line):
	// the ease starts from what the person was looking at, and the row's own content can no longer move the list.
	let settled = node.getBoundingClientRect().height;
	const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(() => { if (!animation) settled = node.getBoundingClientRect().height; });
	observer?.observe(node);
	const start = ({ leaving }: { leaving: boolean }) => {
		if (!leaving || animation || typeof node.animate !== 'function') return;
		const duration = leaveDuration();
		const style = getComputedStyle(node);
		const easing = style.getPropertyValue('--ease-snap') || 'ease-out';
		const px = (value: string) => Number.parseFloat(value) || 0;
		const box = settled;
		const height = style.boxSizing === 'border-box' ? box : box - px(style.paddingTop) - px(style.paddingBottom) - px(style.borderBottomWidth) - px(style.borderTopWidth);
		node.style.overflow = 'hidden';
		animation = node.animate(
			[
				{ height: `${height}px`, paddingTop: style.paddingTop, paddingBottom: style.paddingBottom, borderBottomWidth: style.borderBottomWidth, opacity: 1 },
				{ height: '0px', paddingTop: '0px', paddingBottom: '0px', borderBottomWidth: '0px', opacity: 0 },
			],
			{ duration, easing, fill: 'forwards' },
		);
	};
	start(options);
	return { update: start, destroy: () => { observer?.disconnect(); animation?.cancel(); } };
}
