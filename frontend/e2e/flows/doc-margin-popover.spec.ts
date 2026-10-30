import path from 'node:path';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { openSuggestionDocument } from '../support/docSuggestions';

const TEXT = 'Pair qpa qpb together.\n\nA separating paragraph.\n\nFive qfa qfb qfc qfd qfe together.\n\nA final paragraph.';
const groups = [['qpa', 'qpb'], ['qfa', 'qfb', 'qfc', 'qfd', 'qfe']];

test('@flow:doc-margin-popover numbered markers show only their threads in the comment surface', async ({ page, request }, info) => {
	const mobile = info.project.name.includes('mobile');
	const width = mobile ? 393 : 1440;
	await page.setViewportSize({ width, height: mobile ? 852 : 960 });
	await page.emulateMedia({ colorScheme: 'light', reducedMotion: 'reduce' });
	const items = groups.flat().map((quote) => ({ quote, body: `Thread on ${quote}. This longer comment makes the surface scroll when several threads are shown together.`, author: 'person' as const }));
	const opened = await openSuggestionDocument(page, request, TEXT, items);
	let offset = 0;
	for (const group of groups) {
		const ids = opened.ids.slice(offset, offset + group.length);
		offset += group.length;
		const marker = page.locator(`.cm-comment-marker[data-comment-ids*="${ids[0]}"]`);
		await expect(marker).toHaveAttribute('data-comment-ids', ids.join(','));
		await marker.click();
		const surface = mobile ? page.locator('[data-adaptive-drawer] .drawer-panel') : page.locator('[data-popover]');
		await expect(surface).toBeVisible();
		await expect(page.locator('[data-doc-comments-drawer]')).toHaveCount(0);
		const cards = surface.locator('[aria-label="Comment threads"]');
		for (const item of items) {
			if (group.includes(item.quote)) await expect(cards).toContainText(item.body);
			else await expect(cards).not.toContainText(item.body);
		}
		if (group.length === 5) {
			const scroll = mobile ? surface.locator('.drawer-body') : surface;
			const geometry = await scroll.evaluate((el) => {
				const rect = el.getBoundingClientRect();
				const nestedScrollers = [...el.querySelectorAll<HTMLElement>('*')].filter((child) => ['auto', 'scroll'].includes(getComputedStyle(child).overflowY) && child.scrollHeight > child.clientHeight);
				return { scrollHeight: el.scrollHeight, height: el.clientHeight, top: rect.top, bottom: rect.bottom, viewport: innerHeight, nestedScrollers: nestedScrollers.length };
			});
			expect(geometry.scrollHeight).toBeGreaterThan(geometry.height);
			expect(geometry.nestedScrollers).toBe(0);
			expect(geometry.top).toBeGreaterThanOrEqual(0);
			expect(geometry.bottom).toBeLessThanOrEqual(geometry.viewport);
			await scroll.evaluate((el) => { el.scrollTop = el.scrollHeight; });
			await expect.poll(() => scroll.evaluate((el) => el.scrollTop)).toBeGreaterThan(0);
			await scroll.evaluate((el) => { el.scrollTop = 0; });
		}
		// The surface and everything above it have finished entering, so the screenshot shows it as the person sees it.
		const settled = () => surface.evaluate((el) => {
			const ancestors: Element[] = [];
			for (let node: Element | null = el; node; node = node.parentElement) ancestors.push(node);
			const animations = new Set([...el.getAnimations({ subtree: true }), ...ancestors.flatMap((node) => node.getAnimations())]);
			const finite = [...animations].filter((animation) => animation.playState === 'running' && animation.effect?.getComputedTiming().iterations !== Infinity).length;
			return { finite, minimumOpacity: Math.min(...ancestors.map((node) => Number(getComputedStyle(node).opacity))) };
		});
		await expect.poll(settled).toEqual({ finite: 0, minimumOpacity: 1 });
		const directory = process.env.CORSIA_MARGIN_EVIDENCE_DIR;
		if (directory) await page.screenshot({ path: path.join(directory, `margin-${width}-${group.length}.png`), fullPage: true });
		await page.keyboard.press('Escape');
		await expect(surface).not.toBeVisible();
	}
});
