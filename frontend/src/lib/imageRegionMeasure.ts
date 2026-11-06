/** Observe the viewer's region layer so its tag positions follow its displayed size. */
export function observeRegionSize(layer: HTMLElement | undefined, measure: () => void): () => void {
	const observer = typeof ResizeObserver === 'function' ? new ResizeObserver(measure) : null;
	if (layer) observer?.observe(layer);
	return () => observer?.disconnect();
}
