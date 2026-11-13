import { expect, type Locator } from '@playwright/test';

/** A line of discussion must remain visible through every clipping ancestor above the reply field. */
export async function expectDiscussionVisible(thread: Locator, text: string) {
	await expect.poll(() => thread.evaluate((root, sought) => {
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
	}, text)).toBe(true);
}

/** The field stays on screen and unclipped, and its Send takes the click where it lands. */
export async function expectFieldAndSendUsable(card: Locator, field: Locator) {
	const send = card.getByRole('button', { name: 'Send reply' });
	await expect(field).toBeEditable();
	await expect(send).toBeEnabled();
	await expect.poll(() => field.evaluate((el) => {
		const rect = el.getBoundingClientRect();
		// A field taller than the box that scrolls its card cannot show whole: its bottom edge, the line being typed, is what must be in view.
		if (rect.bottom > innerHeight || (rect.height <= innerHeight && rect.top < 0)) return `field ${Math.round(rect.top)}-${Math.round(rect.bottom)} off screen (viewport ${innerHeight})`;
		if (rect.height < 16) return `field height ${Math.round(rect.height)}`;
		for (let parent = el.parentElement; parent; parent = parent.parentElement) {
			if (!/(auto|scroll|hidden|clip)/.test(getComputedStyle(parent).overflowY)) continue;
			const box = parent.getBoundingClientRect();
			// A field scrolled flush to its scroller's edge can read a fraction of a pixel past it.
			if (rect.bottom > box.bottom + 1 || (rect.height <= box.height && rect.top < box.top - 1)) return `field ${Math.round(rect.top)}-${Math.round(rect.bottom)} clipped by ${parent.className || parent.tagName} ${Math.round(box.top)}-${Math.round(box.bottom)}`;
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
		// Scroll the line itself into the band: scrolling to the end of the region would show a long reply's tail
		// and leave its first line, the one being asserted, clipped above.
		const first = lineOf();
		if (!first) return scrollers.length ? 'line not in the scroll box' : 'line not in the card';
		// Either way: a card whose field is being typed in has already scrolled its first reply above the box.
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

/** The line being typed stays in view and nothing inside the card scrolls but the card: the field grows to its text (it never scrolls itself), exactly one box scrolls the card, and that box's visible edge holds the field's bottom border, its last line and Send (the whole field when it is shorter than the box), with Accept and Reject still taking a tap where they sit. */
export async function expectCaretLineVisible(card: Locator, field: Locator) {
	await expect.poll(() => field.evaluate((el: HTMLTextAreaElement) => {
		if (el.scrollHeight > el.clientHeight + 1) return `field scrolls itself: ${el.clientHeight} of ${el.scrollHeight}`;
		const block = el.closest('[data-autosize-sticky]') as HTMLElement | null;
		const rect = (block ?? el).getBoundingClientRect();
		const send = block?.querySelector('[data-send-reply]')?.getBoundingClientRect() ?? null;
		if (rect.bottom > innerHeight + 1) return `field ${Math.round(rect.top)}-${Math.round(rect.bottom)} off screen (viewport ${innerHeight})`;
		if (send && (send.top < -1 || send.bottom > innerHeight + 1)) return `Send ${Math.round(send.top)}-${Math.round(send.bottom)} off screen`;
		const scrollers: string[] = [];
		// What each box lets through is what the next box out is asked about: a field taller than the box that scrolls it shows only its bottom edge there, and the boxes around that one are not held to the clipped-off part.
		let top = rect.top;
		let bottom = rect.bottom;
		for (let parent = el.parentElement; parent; parent = parent.parentElement) {
			const overflowY = getComputedStyle(parent).overflowY;
			if (!/(auto|scroll|hidden|clip)/.test(overflowY)) continue;
			const box = parent.getBoundingClientRect();
			if (bottom > box.bottom + 1 || (bottom - top <= box.height && top < box.top - 1)) return `field ${Math.round(top)}-${Math.round(bottom)} outside ${parent.className || parent.tagName} ${Math.round(box.top)}-${Math.round(box.bottom)}`;
			top = Math.max(top, box.top);
			bottom = Math.min(bottom, box.bottom);
			if (send && (send.top < box.top - 1 || send.bottom > box.bottom + 1)) return `Send ${Math.round(send.top)}-${Math.round(send.bottom)} outside ${parent.className || parent.tagName} ${Math.round(box.top)}-${Math.round(box.bottom)}`;
			if (/(auto|scroll)/.test(overflowY) && parent.scrollHeight > parent.clientHeight + 1 && !parent.matches('[data-popover], [data-adaptive-drawer]') ) scrollers.push(parent.className || parent.tagName);
		}
		if (scrollers.length > 1) return `more than one box scrolls the card: ${scrollers.join(', ')}`;
		return true;
	})).toBe(true);
	for (const name of [/^Accept/, /^Reject/]) {
		if (!(await card.getByRole('button', { name }).count())) continue;
		const button = card.getByRole('button', { name }).first();
		await expect.poll(() => button.evaluate((el) => {
			const r = el.getBoundingClientRect();
			if (r.top < 0 || r.bottom > innerHeight) return `at ${Math.round(r.top)}-${Math.round(r.bottom)} off screen`;
			const hit = document.elementFromPoint(r.left + r.width / 2, r.top + r.height / 2);
			return !!hit && (hit === el || el.contains(hit)) ? true : 'covered';
		})).toBe(true);
	}
}
