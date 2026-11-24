/**
 * Reports whether `measure` (the natural size of some content) fits inside `box`, now and whenever either resizes.
 * Returns the function that stops watching. Used where a label shows only when its room allows it.
 */
export function observeFit(box: HTMLElement, measure: HTMLElement, onChange: (fits: boolean) => void): () => void {
	const update = () => onChange(measure.getBoundingClientRect().width <= box.getBoundingClientRect().width);
	update();
	const observer = typeof ResizeObserver === 'undefined' ? null : new ResizeObserver(update);
	observer?.observe(box);
	observer?.observe(measure);
	return () => observer?.disconnect();
}
