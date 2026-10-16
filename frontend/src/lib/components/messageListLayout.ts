/**
 * Re-pin the live edge when the composer's floater or the attention rail grows
 * under a transcript the user was pinned to. Returns the observer's cleanup.
 */
export function observeLayoutGrowth(
	messagesEl: HTMLElement | undefined,
	wasPinned: () => boolean,
	followBottom: () => void,
): (() => void) | undefined {
	if (typeof ResizeObserver === 'undefined' || !messagesEl) return;
	// This transcript's own reserved band: its container holds its composer.
	// Reading a sibling's floater would re-pin it from another panel (#1949).
	const container = messagesEl.parentElement;
	const layoutOwners = [
		container?.querySelector<HTMLElement>('.input-floater'),
		container?.querySelector<HTMLElement>('.chat-attention-stack'),
	].filter((element): element is HTMLElement => element !== null);
	if (layoutOwners.length === 0) return;
	const heights = new Map(layoutOwners.map((element) => [element, element.getBoundingClientRect().height]));
	// The attention slot publishes its reservation from its own ResizeObserver,
	// which may run after this one; following now would pin to the old bottom
	// and leave the last row under the band. Follow once that frame has laid out.
	let pendingFollow: number | null = null;
	const observer = new ResizeObserver((entries) => {
		let grew = false;
		for (const { target } of entries) {
			const element = target as HTMLElement;
			const height = element.getBoundingClientRect().height;
			const previous = heights.get(element) ?? height;
			heights.set(element, height);
			grew ||= height > previous;
		}
		if (!grew || !wasPinned() || pendingFollow !== null) return;
		pendingFollow = requestAnimationFrame(() => {
			pendingFollow = null;
			followBottom();
		});
	});
	for (const owner of layoutOwners) observer.observe(owner);
	return () => {
		observer.disconnect();
		if (pendingFollow !== null) cancelAnimationFrame(pendingFollow);
	};
}

/** F7: what an open lands on, just under the header: the visit divider, else the block carrying `new`. */
export function landOnNewContent(messagesEl: HTMLElement | undefined): void {
	(messagesEl?.querySelector('[data-visit-divider]') ?? messagesEl?.querySelector('[data-work-block][data-new]'))
		?.scrollIntoView({ block: 'start' });
}

export function hasLandingTarget(entries: readonly { kind: string; new?: boolean }[]): boolean {
	return entries.some((entry) => entry.kind === 'divider' || (entry.kind === 'fold' && entry.new === true));
}
