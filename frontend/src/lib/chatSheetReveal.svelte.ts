import { tick, untrack } from 'svelte';
import { capSheetAboveText, keptWhole } from '$lib/sheetClearance';
import { transcriptBounds } from '$lib/components/chatCommentViewport';

/**
 * What an opened comment's sheet over the chat transcript needs beyond `keepAboveSheet` (which lifts the transcript to put text above any
 * sheet): the text the person pressed (`pressedText`: the whole highlight up to three lines, else the pressed line; forgotten when the
 * sheet closes, so a comment opened another way is not placed from an old press), the sheet's height capped to leave that text room, and
 * the focus move between the list and one of its threads: into a thread it lands on Back, back to the list it returns to that thread's
 * Reply. Call it from a component's setup.
 */
export function createChatSheetReveal(panel: () => HTMLElement | null, root: () => HTMLElement | null, mode: () => 'list' | 'thread' | null, commentId: () => string | undefined) {
	let press: { el: HTMLElement; line: number } | null = null;
	let shownMode: 'list' | 'thread' | null = null;
	function pressedText(): DOMRect | null {
		const rects = press?.el.isConnected ? [...press.el.getClientRects()] : [];
		const pressed = rects[Math.min(press?.line ?? 0, rects.length - 1)];
		if (!pressed) return null;
		const first = rects[0], last = rects[rects.length - 1];
		return keptWhole(last.bottom - first.top, pressed.height) ? new DOMRect(first.left, first.top, first.width, last.bottom - first.top) : pressed;
	}
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
		if (!sheet) { press = null; return; }
		if (!scroller) return;
		const cap = () => {
			const text = pressedText();
			if (text) capSheetAboveText(sheet, { height: text.height, regionTop: transcriptBounds(scroller)?.top ?? scroller.getBoundingClientRect().top });
		};
		const frame = requestAnimationFrame(cap);
		const observer = new ResizeObserver(() => requestAnimationFrame(cap));
		observer.observe(sheet);
		return () => { cancelAnimationFrame(frame); observer.disconnect(); };
	});
	return { pressedText, notePress(el: HTMLElement, line: number) { press = { el, line }; } };
}
