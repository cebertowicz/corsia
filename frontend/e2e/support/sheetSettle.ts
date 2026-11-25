import { type Page } from '@playwright/test';

/** Settle a surface's own motion (sheet slide, slot transitions) before anything is read. */
export const settleMotion = (page: Page) => page.evaluate(() => Promise.all(document.getAnimations()
	.filter((a) => a.effect?.getComputedTiming().iterations !== Infinity).map((a) => a.finished.catch(() => undefined))).then(() => new Promise<void>((r) => requestAnimationFrame(() => requestAnimationFrame(() => r())))));

/** Wait until the surface's box has not moved for six frames (a sheet still rising or resizing makes every box read stale). */
export const settleRect = (page: Page, root: string) => page.evaluate(async (selector) => {
	const key = () => { const roots = [...document.querySelectorAll<HTMLElement>(selector)]; const r = roots[roots.length - 1]?.getBoundingClientRect(); return r ? `${r.x},${r.y},${r.width},${r.height}` : ''; };
	let last = key(), stable = 0;
	for (let frames = 0; frames < 240 && stable < 6; frames += 1) {
		await new Promise((r) => requestAnimationFrame(r));
		const now = key();
		stable = now === last ? stable + 1 : 0;
		last = now;
	}
}, root);
