/**
 * Whether the document header keeps the review line and the tool cluster on one row, or stacks them (tools first, the line
 * under them). The header's first column gets what the tools and the presence row leave; the line needs its own floor there:
 * its avatar, the name down to its floor, a 16-character request, the chevron and the gaps between them. Nothing about the
 * tools' width is assumed: it is read from the cluster as drawn, so a cluster with the delivery switch and a text History
 * button stacks the line at a wider header than one with only a count and Request review. A short viewport (a phone held
 * sideways) keeps the header as low as it was: it stacks only below 408px of header, never for room.
 */
export type HeaderRoom = {
	/** The header's content width. */
	content: number;
	/** What the other columns hold (the tool cluster in one row, plus the presence row). */
	others: number;
	/** The header's two column gaps. */
	gaps: number;
	/** What the review line needs in the first column. */
	floor: number;
};

export function fitsOneRow(room: HeaderRoom): boolean {
	return room.content - room.others - room.gaps >= room.floor;
}

/** A viewport this short is a phone held sideways: every pixel of header height is taken from the document. */
const SHORT_HEIGHT = 420;
export const SHORT_VIEWPORT = `(max-height: ${SHORT_HEIGHT}px)`;
/** The header width under which the line always takes its own row, however short the viewport. */
const NARROW_HEADER = 408;

/** Whether the header stacks. A short viewport never grows the header for room: only a header too narrow to hold the line at all stacks. */
export function shouldStack(room: HeaderRoom, short: boolean): boolean {
	return short ? room.content < NARROW_HEADER : !fitsOneRow(room);
}

const px = (value: string): number => Number.parseFloat(value) || 0;

/** The line's floor, from the line as drawn: the same floor whether it is one row or stacked, expanded or not. */
function lineFloor(line: HTMLElement): number {
	const probe = document.createElement('span');
	probe.style.cssText = 'position:absolute;visibility:hidden;pointer-events:none;height:0;padding:0;border:0';
	line.append(probe);
	probe.style.width = '16ch';
	const ask = probe.getBoundingClientRect().width;
	probe.style.width = 'var(--space-2)';
	const gap = probe.getBoundingClientRect().width;
	probe.remove();
	const avatar = line.querySelector('.rb-lead')?.firstElementChild?.getBoundingClientRect().width ?? 0;
	const who = line.querySelector<HTMLElement>('.rb-who');
	// A cut name has its own floor (`min-width`); a name that is not cut keeps its natural width.
	const whoFloor = who ? (px(getComputedStyle(who).minWidth) || who.scrollWidth) : 0;
	const chevron = line.querySelector('.rb-chev')?.getBoundingClientRect().width ?? 0;
	return avatar + (who ? gap + whoFloor : 0) + gap + ask + (chevron ? gap + chevron : 0);
}

/** A notice or receipt has no line: it needs about a request's worth of room beside the tools. */
function noticeFloor(node: HTMLElement): number {
	const probe = document.createElement('span');
	probe.style.cssText = 'position:absolute;visibility:hidden;pointer-events:none;height:0;padding:0;border:0;width:16ch';
	node.append(probe);
	const width = probe.getBoundingClientRect().width;
	probe.remove();
	return width;
}

export function measureHeader(node: HTMLElement): HeaderRoom | null {
	const slot = node.querySelector('.rb-slot');
	const tools = node.querySelector<HTMLElement>('.document-tools');
	if (!slot || !tools) return null;
	const style = getComputedStyle(node);
	const toolsStyle = getComputedStyle(tools);
	const toolGap = px(toolsStyle.columnGap);
	const toolWidth = [...tools.children].reduce((sum, child) => sum + child.getBoundingClientRect().width, 0) + toolGap * Math.max(0, tools.children.length - 1);
	const presence = node.querySelector('.presence-row')?.getBoundingClientRect().width ?? 0;
	const line = node.querySelector<HTMLElement>('.review-bar.ask .rb-line');
	return {
		content: node.clientWidth - px(style.paddingLeft) - px(style.paddingRight),
		others: toolWidth + presence,
		gaps: 2 * px(style.columnGap),
		floor: line ? lineFloor(line) : noticeFloor(node),
	};
}

/** Marks the header `rb-stack` while the one-row layout does not fit. Re-measured on any size change and when `key` changes. */
export function headerStack(node: HTMLElement, _key?: unknown) {
	let frame = 0;
	const apply = () => {
		frame = 0;
		const room = measureHeader(node);
		node.classList.toggle('rb-stack', !!room && shouldStack(room, short.matches));
	};
	const schedule = () => { if (!frame) frame = requestAnimationFrame(apply); };
	// Only the viewport's height changes with the orientation; the header's own width may not.
	const short = window.matchMedia(SHORT_VIEWPORT);
	short.addEventListener('change', schedule);
	const observer = new ResizeObserver(schedule);
	observer.observe(node);
	// The tools change width without the header changing (a count, a label); the decision does not depend on the layout it picks, so watching them cannot loop.
	const tools = node.querySelector('.document-tools');
	if (tools) observer.observe(tools);
	// First paint is already decided.
	apply();
	return {
		update: schedule,
		destroy() {
			observer.disconnect();
			short.removeEventListener('change', schedule);
			if (frame) cancelAnimationFrame(frame);
			node.classList.remove('rb-stack');
		},
	};
}
