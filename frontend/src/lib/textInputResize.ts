/**
 * Grow a text entry surface to its content until the shared composer limit,
 * then keep the overflow available inside the control.
 *
 * The measure needs `height: auto`, which a CSS height transition cannot interpolate
 * from or to. So the measure runs with transitions off, the element goes back to the
 * height it was showing, and only then does the target height go on: a surface that
 * transitions its height grows from where it is, even mid-transition.
 */
export function resizeTextInputToContent(element: HTMLElement, maxHeight: number): number {
	const shown = element.style.height ? element.getBoundingClientRect().height : 0;
	const previousTransition = element.style.transition;
	element.style.transition = 'none';
	element.style.height = 'auto';
	const contentHeight = element.scrollHeight;
	const height = Math.min(contentHeight, maxHeight);
	if (shown > 0) {
		element.style.height = `${shown}px`;
		void element.offsetHeight;
	}
	element.style.transition = previousTransition;
	element.style.height = `${height}px`;
	element.style.overflowY = contentHeight > maxHeight ? 'auto' : 'hidden';
	return height;
}
