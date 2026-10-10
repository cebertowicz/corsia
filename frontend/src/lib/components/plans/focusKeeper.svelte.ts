/**
 * Keeps keyboard focus in the tree when the row holding it leaves. A row that
 * moves out of the Scheduled group, a group that dissolves under its toggle, or
 * a member acted on from its menu is rebuilt elsewhere, and the browser drops
 * focus to the page. Focus instead moves to the row now at the same position:
 * the next row, or the one remaining member.
 */

const rowsOf = (root: HTMLElement): HTMLElement[] => [...root.querySelectorAll<HTMLElement>('.tree-row')];

export function createFocusKeeper(element: () => HTMLElement | null, rendered: () => unknown): void {
	let held = -1;
	// Before the DOM updates: where in the tree's rows did focus sit?
	$effect.pre(() => {
		void rendered();
		const root = element();
		const active = document.activeElement;
		held = root && active && root.contains(active) ? rowsOf(root).findIndex((row) => row.contains(active)) : -1;
	});
	// After: if focus was lost with the row, hand it to the row that took its place.
	$effect(() => {
		void rendered();
		const root = element();
		const was = held;
		held = -1;
		if (was < 0 || !root || (document.activeElement && document.activeElement !== document.body)) return;
		const rows = rowsOf(root);
		rows[Math.min(was, rows.length - 1)]?.querySelector<HTMLElement>('button')?.focus();
	});
}
