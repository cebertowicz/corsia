import { expect, type Locator } from '@playwright/test';

/** Whether a line of the card's text draws on screen, above its reply field and inside every box that clips it; a reason when not. */
function lineVisibility(root: Element, sought: string): boolean | string {
	const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
	let node: Text | null;
	while ((node = walker.nextNode() as Text | null) && !node.data.includes(sought)) {}
	if (!node) return false;
	const start = node.data.indexOf(sought), range = document.createRange();
	range.setStart(node, start); range.setEnd(node, start + sought.length);
	const line = range.getBoundingClientRect();
	const composer = root.querySelector('.ct-compose')?.getBoundingClientRect();
	if (!line.height) return 'line has no height';
	if (line.top < 0 || line.bottom > innerHeight) return `line ${Math.round(line.top)}-${Math.round(line.bottom)} off screen (viewport ${innerHeight})`;
	if (composer && line.bottom > composer.top) return `line ${Math.round(line.top)}-${Math.round(line.bottom)} under the composer at ${Math.round(composer.top)}`;
	for (let parent = node.parentElement; parent; parent = parent.parentElement) {
		if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(parent).overflowY)) continue;
		const box = parent.getBoundingClientRect();
		if (line.top < box.top || line.bottom > box.bottom) return `line ${Math.round(line.top)}-${Math.round(line.bottom)} clipped by ${parent.className || parent.tagName} ${Math.round(box.top)}-${Math.round(box.bottom)}`;
	}
	return true;
}

/** A line of discussion must remain visible through every clipping ancestor above the reply field. */
export async function expectDiscussionVisible(thread: Locator, text: string) {
	await expect.poll(() => thread.evaluate(lineVisibility, text)).toBe(true);
}

/** An idle draft field is one line with its whole placeholder showing and a target at least 24px tall that takes a click at its centre. */
export async function expectIdleFieldUsable(field: Locator) {
	await expect.poll(() => field.evaluate(el => {
		const input = el as HTMLTextAreaElement;
		const style = getComputedStyle(input);
		const box = input.getBoundingClientRect();
		if (box.height < 24) return `field is ${box.height.toFixed(1)}px tall, under the 24px target`;
		const inner = box.height - (Number.parseFloat(style.paddingTop) || 0) - (Number.parseFloat(style.paddingBottom) || 0) - (Number.parseFloat(style.borderTopWidth) || 0) - (Number.parseFloat(style.borderBottomWidth) || 0);
		const line = Number.parseFloat(style.lineHeight) || 0;
		if (input.value || inner > line * 1.5 + 1) return `field is not one empty line: ${inner.toFixed(1)}px of ${line}px lines`;
		const canvas = document.createElement('canvas').getContext('2d')!;
		canvas.font = `${style.fontStyle} ${style.fontWeight} ${style.fontSize} ${style.fontFamily}`;
		const width = canvas.measureText(input.placeholder).width;
		const room = input.clientWidth - (Number.parseFloat(style.paddingLeft) || 0) - (Number.parseFloat(style.paddingRight) || 0);
		if (width > room) return `placeholder "${input.placeholder}" is ${width.toFixed(1)}px in ${room.toFixed(1)}px: it would clip`;
		const hit = document.elementFromPoint(box.left + box.width / 2, box.top + box.height / 2);
		return hit === input ? true : `the field's centre takes the click on ${(hit as HTMLElement | null)?.className || hit?.tagName}`;
	})).toBe(true);
}

/** The newest reply is whole where the thread opened: every line box of it is on screen and inside every box that clips it. */
export async function expectNewestReplyWhole(card: Locator) {
	await expect.poll(() => card.evaluate(root => {
		const newest = root.querySelector<HTMLElement>('[data-discussion-replies] .ct-reply:last-child');
		if (!newest) return 'no newest reply';
		const walker = document.createTreeWalker(newest, NodeFilter.SHOW_TEXT);
		let lines = 0;
		for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
			if (!node.data.trim()) continue;
			const range = document.createRange();
			range.selectNodeContents(node);
			for (const rect of range.getClientRects()) {
				if (!rect.height) continue;
				lines++;
				const line = `newest reply line ${rect.top.toFixed(1)}-${rect.bottom.toFixed(1)} ("${node.data.trim().slice(0, 24)}")`;
				if (rect.top < -1 || rect.bottom > innerHeight + 1) return `${line} is off screen (viewport ${innerHeight})`;
				for (let parent = node.parentElement; parent; parent = parent.parentElement) {
					if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(parent).overflowY)) continue;
					const box = parent.getBoundingClientRect();
					if (rect.top < box.top - 1 || rect.bottom > box.bottom + 1) return `${line} is clipped by ${parent.className || parent.tagName} ${box.top.toFixed(1)}-${box.bottom.toFixed(1)}`;
				}
			}
		}
		return lines > 0 ? true : 'newest reply has no line boxes';
	})).toBe(true);
}

/** A comment card never scrolls inside itself, its reply field included: only the sheet, popover or column holding it scrolls. */
export async function expectNoCardScroller(card: Locator) {
	await expect.poll(() => card.evaluate(root => [root, ...root.querySelectorAll<HTMLElement>('*')]
		.filter(el => /auto|scroll/.test(getComputedStyle(el).overflowY) && el.scrollHeight > el.clientHeight + 1)
		.map(el => `${el.tagName.toLowerCase()}.${[...el.classList].join('.')}`))).toEqual([]);
}

/**
 * A text change that leaves the field's height as it was (a word added to the last line, a draft put back) refits the
 * field without moving what holds it: the refit's measurement must not leave the sheet scrolled somewhere else.
 */
export async function expectRefitKeepsScroll(field: Locator) {
	const moves = await field.evaluate(async (el) => {
		const input = el as HTMLTextAreaElement;
		let port: HTMLElement | null = input.parentElement;
		while (port && !/auto|scroll/.test(getComputedStyle(port).overflowY)) port = port.parentElement;
		const holder = port ?? document.scrollingElement as HTMLElement;
		const text = input.value, seen: number[] = [];
		for (const value of [`${text} x`, text]) {
			const before = holder.scrollTop;
			input.value = value;
			input.dispatchEvent(new Event('input', { bubbles: true }));
			await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
			seen.push(holder.scrollTop - before);
		}
		return seen;
	});
	expect(moves).toEqual([0, 0]);
}

/**
 * The field grows with its text and never scrolls itself; its end (the caret line) stays on screen and unclipped, and
 * its Send takes the click where it lands. A field taller than the screen is read by scrolling what holds the card.
 */
export async function expectFieldAndSendUsable(card: Locator, field: Locator) {
	const send = card.getByRole('button', { name: 'Send reply' });
	await expect(field).toBeEditable();
	await expect(send).toBeEnabled();
	await expect.poll(() => field.evaluate((el) => {
		const input = el as HTMLTextAreaElement;
		if (input.scrollHeight > input.clientHeight + 1) return `field scrolls itself (${input.scrollHeight} in ${input.clientHeight})`;
		const rect = el.getBoundingClientRect(), line = Number.parseFloat(getComputedStyle(el).lineHeight) || 20;
		const caret = { top: rect.bottom - line, bottom: rect.bottom };
		if (caret.top < -1 || caret.bottom > innerHeight + 1) return `caret line ${Math.round(caret.top)}-${Math.round(caret.bottom)} off screen (viewport ${innerHeight})`;
		for (let parent = el.parentElement; parent; parent = parent.parentElement) {
			if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(parent).overflowY)) continue;
			const box = parent.getBoundingClientRect();
			if (caret.top < box.top - 1 || caret.bottom > box.bottom + 1) return `caret line ${Math.round(caret.top)}-${Math.round(caret.bottom)} clipped by ${parent.className || parent.tagName} ${Math.round(box.top)}-${Math.round(box.bottom)}`;
		}
		return true;
	})).toBe(true);
	await expect.poll(() => send.evaluate((el) => {
		const rect = el.getBoundingClientRect();
		if (rect.width < 8 || rect.height < 8) return false;
		if (rect.top < 0 || rect.bottom > innerHeight || rect.left < 0 || rect.right > innerWidth) return false;
		const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
		return !!hit && (hit === el || el.contains(hit));
	})).toBe(true);
}

/**
 * A card that sits in a scrolling page (a margin column, a transcript) and is taller than the screen: the page holding it
 * is scrolled until the sought line is in view, as a person scrolls up to read, and two frames later the line still draws
 * whole there. Each try scrolls again, so a reveal of the field still settling from the last keystroke is not mistaken for
 * the page refusing to scroll; a page that keeps pulling the line away fails.
 */
export async function expectReachableByOuterScroll(card: Locator, text: string) {
	await expect.poll(async () => {
		const scrolled = await card.evaluate(async (root, sought) => {
			const walker = document.createTreeWalker(root, NodeFilter.SHOW_TEXT);
			let node: Text | null;
			while ((node = walker.nextNode() as Text | null) && !node.data.includes(sought)) {}
			if (!node) return 'line not in the card';
			let scroller: HTMLElement | null = root.parentElement;
			while (scroller && !(/(auto|scroll)/.test(getComputedStyle(scroller).overflowY) && scroller.scrollHeight > scroller.clientHeight + 1)) scroller = scroller.parentElement;
			const range = document.createRange(), start = node.data.indexOf(sought);
			range.setStart(node, start); range.setEnd(node, start + sought.length);
			if (scroller) {
				const view = scroller.getBoundingClientRect();
				scroller.scrollTop += range.getBoundingClientRect().top - (view.top + view.height / 3);
			}
			await new Promise((done) => requestAnimationFrame(() => requestAnimationFrame(done)));
			return true;
		}, text);
		return scrolled === true ? card.evaluate(lineVisibility, text) : scrolled;
	}).toBe(true);
}

/** Where the card's room cannot show the whole thread, it must be reachable: the box that scrolls the card is scrolled until the sought line sits in the band above the pinned composer, and the line then draws there. With nothing to scroll, the line must already draw. */
export async function expectDiscussionReachableByScroll(card: Locator, text: string) {
	await expect.poll(() => card.evaluate((root, sought) => {
		const composerTop = (): number => root.querySelector('.ct-compose')?.getBoundingClientRect().top ?? innerHeight;
		const region = root.querySelector('[data-discussion-replies]') as HTMLElement | null;
		if (!region) return 'no reply region';
		const scrollerOf = (node: HTMLElement | null, stop: HTMLElement | null): HTMLElement[] => {
			const found: HTMLElement[] = [];
			for (let n = node; n && n !== stop; n = n.parentElement) if (/(auto|scroll)/.test(getComputedStyle(n).overflowY) && n.scrollHeight > n.clientHeight + 8) found.push(n);
			return found;
		};
		// Only the card's own surface may be scrolled — the card, its inner regions, and the popover or drawer panel
		// holding it. Scrolling the surface behind a popover moves its anchor and dismisses the card, which is the
		// opposite of the reachability being asked for.
		const surface = root.closest('[data-popover], [data-adaptive-drawer]') as HTMLElement | null;
		const stop = surface ? surface.parentElement : root.parentElement;
		const scrollers = scrollerOf(region, stop);
		// The outermost box is the card's own port: a region sitting behind the pinned composer only comes into the
		// band when the port moves. The innermost one then brings the line itself into view inside its region.
		const port = scrollers[scrollers.length - 1] ?? null, inner = scrollers[0] ?? null;
		const lineOf = (): Range | null => {
			const walker = document.createTreeWalker(inner ?? port ?? root, NodeFilter.SHOW_TEXT);
			let node: Text | null;
			while ((node = walker.nextNode() as Text | null) && !node.data.includes(sought)) {}
			if (!node) return null;
			const start = node.data.indexOf(sought), range = document.createRange();
			range.setStart(node, start); range.setEnd(node, start + sought.length);
			return range;
		};
		if (port) {
			const raised = region.getBoundingClientRect().bottom - (composerTop() - 8);
			if (raised > 0) port.scrollTop += raised;
		}
		// Scroll the line itself to the top of its scroll box, up or down: scrolling to the end of the region would show a
		// long reply's tail and leave its first line, the one being asserted, clipped above, and a long draft below the
		// discussion leaves the box scrolled past it.
		const first = lineOf();
		if (!first) return scrollers.length ? 'line not in the scroll box' : 'line not in the card';
		if (inner) inner.scrollTop += first.getBoundingClientRect().top - inner.getBoundingClientRect().top - 2;
		const range = lineOf() ?? first;
		const line = range.getBoundingClientRect();
		if (!line.height) return 'line has no height';
		if (inner && line.height > inner.clientHeight) return `line ${Math.round(line.height)} taller than its scroll box ${Math.round(inner.clientHeight)}`;
		if (line.top < 0 || line.bottom > Math.min(innerHeight, composerTop())) {
			const b = inner?.getBoundingClientRect(), p = port?.getBoundingClientRect();
			return `line ${Math.round(line.top)}-${Math.round(line.bottom)} above 0 below ${Math.round(Math.min(innerHeight, composerTop()))} inner=${inner?.className ?? 'none'} ${b ? `${Math.round(b.top)}-${Math.round(b.bottom)} st=${Math.round(inner!.scrollTop)} sh=${inner!.scrollHeight} ch=${inner!.clientHeight}` : ''} port=${port ? `${Math.round(p!.top)}-${Math.round(p!.bottom)} st=${Math.round(port.scrollTop)} sh=${port.scrollHeight} ch=${port.clientHeight}` : 'none'} region=${Math.round(region.getBoundingClientRect().top)}-${Math.round(region.getBoundingClientRect().bottom)}`;
		}
		for (let parent = range.startContainer.parentElement; parent; parent = parent.parentElement) {
			if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(parent).overflowY)) continue;
			const clip = parent.getBoundingClientRect();
			if (line.top < clip.top - 1 || line.bottom > clip.bottom + 1) return `line ${Math.round(line.top)}-${Math.round(line.bottom)} clipped by ${parent.className || parent.tagName} ${Math.round(clip.top)}-${Math.round(clip.bottom)}`;
		}
		return true;
	}, text)).toBe(true);
}
