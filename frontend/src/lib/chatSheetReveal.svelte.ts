import { tick, untrack } from 'svelte';
import { capSheetAboveText, keptWhole, SHEET_GAP } from '$lib/sheetClearance';
import { restingTop } from '$lib/cm/visible-region';

/**
 * A sheet over the chat transcript covers its lower part. It rises only as far as leaves the pressed highlight above it (up to three
 * lines of it, else the pressed line), and the transcript is lifted to put that text there, with scroll room added below when it ends
 * under the sheet. Opening and any later resize of the sheet only ever lift text it newly covers. `notePress` keeps the line the
 * person pressed. It also moves focus between the list and one of its threads: into a thread it lands on Back, back to the list it returns to that thread's Reply. Call it from a component's setup.
 */
export function createChatSheetReveal(panel: () => HTMLElement | null, root: () => HTMLElement | null, mode: () => 'list' | 'thread' | null, commentId: () => string | undefined) {
	let press: { el: HTMLElement; line: number } | null = null;
	let shownMode: 'list' | 'thread' | null = null;
	$effect(() => {
		const now = mode(), id = commentId();
		untrack(() => {
			const previous = shownMode; shownMode = now;
			const focusLater = (selector: string) => void tick().then(() => requestAnimationFrame(() => document.querySelector<HTMLElement>(selector)?.focus()));
			if (previous === 'list' && now === 'thread') focusLater('.thread-back');
			else if (previous === 'thread' && now === 'list') focusLater(`[data-reply-comment="${id}"]`);
		});
	});
	$effect(() => {
		const sheet = panel(), scroller = root();
		if (!sheet || !scroller) return;
		const padding = scroller.style.paddingBottom;
		const lift = () => {
			const el = press?.el;
			if (!el?.isConnected || !sheet.isConnected) return;
			const rects = [...el.getClientRects()];
			const pressed = rects[Math.min(press!.line, rects.length - 1)];
			if (!pressed) return;
			const first = rects[0], last = rects[rects.length - 1];
			const whole = keptWhole(last.bottom - first.top, pressed.height);
			const text = whole ? { top: first.top, bottom: last.bottom } : { top: pressed.top, bottom: pressed.bottom };
			const regionTop = scroller.getBoundingClientRect().top;
			capSheetAboveText(sheet, { height: text.bottom - text.top, regionTop });
			const top = restingTop(sheet) ?? sheet.getBoundingClientRect().top;
			if (text.top >= top + sheet.offsetHeight) return;
			const over = text.bottom + SHEET_GAP - top;
			if (over <= 0) return;
			const room = scroller.scrollHeight - scroller.clientHeight - scroller.scrollTop;
			if (room < over) scroller.style.paddingBottom = `calc(${getComputedStyle(scroller).paddingBottom} + ${Math.ceil(over - room)}px)`;
			scroller.scrollTop += over;
		};
		const frame = requestAnimationFrame(lift);
		const observer = new ResizeObserver(() => requestAnimationFrame(lift));
		observer.observe(sheet);
		return () => { cancelAnimationFrame(frame); observer.disconnect(); scroller.style.paddingBottom = padding; };
	});
	return { notePress(el: HTMLElement, line: number) { press = { el, line }; } };
}
