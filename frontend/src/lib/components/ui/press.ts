import './press.css';

/**
 * Press feedback for a toggle that opens an `Expand`: the control tints on press-down and
 * releases on up or cancel. The pressed state is an attribute set by a pointer listener, so
 * it shows within a frame on iOS, where `:active` only fires once a touch listener exists.
 * The tint is an overlay's opacity; nothing about the control's size or position changes.
 */
export function press(node: HTMLElement): { destroy(): void } {
	if (getComputedStyle(node).position === 'static') node.style.position = 'relative';
	node.setAttribute('data-press', '');
	const down = () => node.setAttribute('data-pressing', '');
	const up = () => node.removeAttribute('data-pressing');
	const noop = () => {};
	node.addEventListener('pointerdown', down);
	node.addEventListener('touchstart', noop, { passive: true });
	for (const type of ['pointerup', 'pointercancel', 'pointerleave']) node.addEventListener(type, up);
	return {
		destroy() {
			node.removeEventListener('pointerdown', down);
			node.removeEventListener('touchstart', noop);
			for (const type of ['pointerup', 'pointercancel', 'pointerleave']) node.removeEventListener(type, up);
		},
	};
}
