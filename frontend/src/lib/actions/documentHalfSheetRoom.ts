/**
 * Give the document's half sheet the room its fixed delivery row adds to the list, and the room the heading's second row adds when the
 * Open and Resolved switch wraps under the title (the heading is one row, as tall as Close, unless it wraps).
 */
export function documentHalfSheetRoom(drawer: HTMLElement) {
	const panel = drawer.closest<HTMLElement>('.drawer-panel');
	if (!panel) return;
	let frame = 0;
	const resize = new ResizeObserver(schedule);
	/** The height the heading has beyond one row: its box less Close's row and its bottom padding. */
	function wrappedHeadingRow() {
		const heading = drawer.querySelector<HTMLElement>(':scope > .drawer-heading');
		const tools = heading?.querySelector<HTMLElement>('.drawer-tools');
		if (!heading || !tools) return 0;
		const padding = parseFloat(getComputedStyle(heading).paddingBottom) || 0;
		return Math.max(0, heading.getBoundingClientRect().height - tools.getBoundingClientRect().height - padding);
	}
	function measure() {
		const rows = Array.from(drawer.children).filter((child) => child.matches('.drawer-foot'));
		const nestedFooter = drawer.querySelector('.drawer-comments > .drawer-foot');
		if (nestedFooter) rows.push(nestedFooter);
		const gap = parseFloat(getComputedStyle(drawer).rowGap) || 0;
		const extra = rows.reduce((sum, row) => sum + row.getBoundingClientRect().height + gap, 0) + wrappedHeadingRow();
		panel!.style.setProperty('--drawer-half-extra', `${extra}px`);
	}
	function schedule() {
		cancelAnimationFrame(frame);
		frame = requestAnimationFrame(measure);
	}
	function observeRows() {
		resize.disconnect();
		for (const row of drawer.children) if (row.matches('.drawer-foot, .drawer-heading')) resize.observe(row);
		const nestedFooter = drawer.querySelector('.drawer-comments > .drawer-foot');
		if (nestedFooter) resize.observe(nestedFooter);
		schedule();
	}
	const mutation = new MutationObserver(observeRows);
	mutation.observe(drawer, { childList: true });
	observeRows();
	return { destroy() { cancelAnimationFrame(frame); resize.disconnect(); mutation.disconnect(); panel!.style.removeProperty('--drawer-half-extra'); } };
}
