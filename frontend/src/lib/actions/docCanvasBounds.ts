/** Bound the local canvas against the existing home visualViewport projection. */
export function docCanvasBounds(element: HTMLElement) {
	const measure = () => element.style.setProperty('--doc-canvas-top', `${Math.max(0, element.getBoundingClientRect().top)}px`);
	measure();
	const observer = new ResizeObserver(measure);
	observer.observe(element);
	return { destroy() { observer.disconnect(); } };
}
