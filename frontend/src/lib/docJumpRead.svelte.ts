import { tick } from 'svelte';
import { docJumpTargets, type DocJumpTargets } from './docJumpTargets';
import type { DocComment } from '$lib/stores/docComments';

const NOTHING: DocJumpTargets = { count: 0, target: null, direction: null };

/**
 * What the jump control points at, kept current: one read per frame after anything that moves a line or a card — scrolling,
 * the text, the comments, the column — the same way chat's control watches its transcript. `root` answers undefined when
 * the surface has no room for the control (covered by a draft, a sheet up), which is the same as having nothing to point at.
 */
export function createJumpRead(root: () => HTMLElement | undefined, threads: () => DocComment[]) {
	let read = $state<DocJumpTargets>(NOTHING);
	$effect(() => {
		const canvas = root();
		void threads();
		if (!canvas) { read = NOTHING; return; }
		let frame = 0;
		// A scroll moves every line equally, so most frames answer the same thing: only a different thread, side or total re-renders.
		const write = (next: DocJumpTargets) => {
			const was = read;
			if (was.count === next.count && was.direction === next.direction && was.target?.id === next.target?.id) return;
			read = next;
		};
		const schedule = () => { cancelAnimationFrame(frame); frame = requestAnimationFrame(() => { frame = 0; write(docJumpTargets(canvas, threads())); }); };
		const observer = new MutationObserver(schedule);
		observer.observe(canvas, { childList: true, subtree: true, attributes: true });
		// The element that scrolls is not the same surface twice: the document pans its CodeMirror scroller, the image
		// viewer pans its own viewport. Naming either class would make this read know who is mounting it, and neither
		// mount site holds the element to pass in, so listen on the root in the capture phase instead - a scroll on any
		// descendant reaches it, whoever ends up scrolling. The schedule is one frame deep and answers nothing when the
		// answer is unchanged, so hearing a scroll that moves no line costs a frame.
		canvas.addEventListener('scroll', schedule, { capture: true, passive: true });
		void tick().then(schedule);
		return () => {
			observer.disconnect();
			canvas.removeEventListener('scroll', schedule, { capture: true });
			cancelAnimationFrame(frame);
		};
	});
	return () => read;
}
