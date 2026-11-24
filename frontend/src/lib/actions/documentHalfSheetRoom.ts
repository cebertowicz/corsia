/** Give the document's half sheet the room its fixed status and delivery rows add to the list. */
export function documentHalfSheetRoom(drawer: HTMLElement) {
	const panel = drawer.closest<HTMLElement>('.drawer-panel');
	if (!panel) return;
	let frame = 0;
	const resize = new ResizeObserver(schedule);
	function measure() {
		const rows = Array.from(drawer.children).filter((child) => child.matches('.drawer-switch, .drawer-foot'));
		const nestedFooter = drawer.querySelector('.drawer-comments > .drawer-foot');
		if (nestedFooter) rows.push(nestedFooter);
		const gap = parseFloat(getComputedStyle(drawer).rowGap) || 0;
		const extra = rows.reduce((sum, row) => sum + row.getBoundingClientRect().height + gap, 0);
		panel!.style.setProperty('--drawer-half-extra', `${extra}px`);
	}
	function schedule() {
		cancelAnimationFrame(frame);
		frame = requestAnimationFrame(measure);
	}
	function observeRows() {
		resize.disconnect();
		for (const row of drawer.children) if (row.matches('.drawer-switch, .drawer-foot')) resize.observe(row);
		const nestedFooter = drawer.querySelector('.drawer-comments > .drawer-foot');
		if (nestedFooter) resize.observe(nestedFooter);
		schedule();
	}
	const mutation = new MutationObserver(observeRows);
	mutation.observe(drawer, { childList: true });
	observeRows();
	return { destroy() { cancelAnimationFrame(frame); resize.disconnect(); mutation.disconnect(); panel!.style.removeProperty('--drawer-half-extra'); } };
}
