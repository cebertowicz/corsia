import type { Page } from '@playwright/test';

/**
 * Samples every painted frame until `keep` returns false; installed on the page by `installPaintedFrames`. `measure`
 * reads the page as the frame paints it, and `keep` records that reading once the frame is done.
 */
export type EachPaintedFrame = <T>(measure: () => T, keep: (sample: T) => boolean) => void;

/** Nesting below the body for the observed sentinel: deeper than any element the app observes. */
const SENTINEL_DEPTH = 128;
/** Resize-observer passes the sampler takes part in per frame; the app's own settle well within it. */
const PASSES = 8;

/**
 * Gives a page-side sampler `window.__eachPaintedFrame(measure, keep)`, where `measure` runs inside each frame's
 * rendering, after the last resize observation the page makes in it, so it sees what that frame paints.
 *
 * A read in a task after the frame (a timer or a posted message) can instead see the page as a later task left it, a
 * network answer that landed after the paint for one, which no frame showed. A read in the frame's first resize pass
 * can see a layout that a later pass corrects before paint: the browser delivers resize observations in passes, each
 * only for elements deeper than the shallowest one the previous pass reported, so an observer of deep elements (an
 * editor's scroller) can run two or three passes after one of a shallow element. So the sampler observes a sentinel
 * deeper than anything the app observes, with a new observer each pass on a sentinel one level deeper than the last:
 * each pass reports it after the app's observers in that pass, `measure` runs in every pass, and the last reading is
 * the painted one. A sentinel never lowers the shallowest depth of a pass, so it never holds back an observation of the
 * app's, and the next is always deep enough to be reported, so none is held back either.
 */
export async function installPaintedFrames(page: Page): Promise<void> {
	await page.evaluate(({ depth, passes }) => {
		const eachPaintedFrame: EachPaintedFrame = (measure, keep) => {
			const holder = document.createElement('div');
			holder.setAttribute('aria-hidden', 'true');
			holder.style.cssText = 'position:fixed;left:0;top:0;width:1px;height:1px;visibility:hidden;pointer-events:none';
			// One sentinel per pass, each a level deeper than the last, so every pass of the frame reaches the next one.
			const sentinels: HTMLElement[] = [];
			let level: HTMLElement = holder;
			for (let at = 0; at < depth + passes; at++) {
				level = level.appendChild(document.createElement('div'));
				level.style.cssText = 'width:1px;height:1px';
				if (at >= depth) sentinels.push(level);
			}
			document.body.append(holder);
			let observer: ResizeObserver | null = null;
			let reading: { sample: ReturnType<typeof measure> } | null = null;
			const commit = new MessageChannel();
			// Runs as a task once the frame's rendering is over: the last pass's reading is the one painted.
			commit.port1.onmessage = () => {
				observer?.disconnect();
				const done = reading;
				reading = null;
				if (done && keep(done.sample)) requestAnimationFrame(frame);
				else {
					holder.remove();
					commit.port1.close();
				}
			};
			const observe = (pass: number) => {
				observer?.disconnect();
				// A new observation reports its target's size once, in the next pass of this frame's rendering.
				observer = new ResizeObserver(() => {
					if (!reading) commit.port2.postMessage(null);
					reading = { sample: measure() };
					if (pass + 1 < passes) observe(pass + 1);
				});
				observer.observe(sentinels[pass]);
			};
			const frame = () => observe(0);
			requestAnimationFrame(frame);
		};
		(window as Window & { __eachPaintedFrame?: EachPaintedFrame }).__eachPaintedFrame = eachPaintedFrame;
	}, { depth: SENTINEL_DEPTH, passes: PASSES });
}
