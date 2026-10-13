/** Responsive plan-tree measurement and its one caller-owned CSS-length conversion. */
export function createPlanTreeLayout(
	element: () => HTMLElement | null,
	indentAt: (depth: number, width: number, reserve: number) => string,
) {
	let treeWidth = $state(0);
	let indentReserve = $state(0);

	$effect(() => {
		const el = element();
		if (!el) return;
		const measure = () => {
			treeWidth = el.clientWidth;
			indentReserve = Number.parseFloat(getComputedStyle(el).getPropertyValue('--plan-indent-reserve')) || 0;
		};
		measure();
		if (typeof ResizeObserver === 'undefined') return;
		const watch = new ResizeObserver(measure);
		watch.observe(el);
		return () => watch.disconnect();
	});

	const depthStyle = (depth: number) => `--depth:${depth};--indent:${indentAt(depth, treeWidth, indentReserve)}`;
	return { indentAt: (depth: number) => indentAt(depth, treeWidth, indentReserve), depthStyle };
}
