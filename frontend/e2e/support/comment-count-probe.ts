import type { Locator, Page, TestInfo } from '@playwright/test';
import { expect } from '@playwright/test';

/** Wait until no finite animation runs and the plan has loaded, so a frame shows the settled screen. */
export async function settled(page: Page) {
	await expect.poll(() => page.evaluate(() => document.getAnimations().filter((animation) =>
		animation.playState === 'running' && animation.effect?.getComputedTiming().iterations !== Infinity,
	).map((animation) => `${(animation as CSSAnimation).animationName ?? animation.constructor.name} on ${((animation.effect as KeyframeEffect)?.target as Element | null)?.getAttribute('class') ?? '?'}`)), { timeout: 30_000 }).toEqual([]);
	await expect(page.getByText('Loading plan…')).toHaveCount(0);
}

/**
 * Switch the emulated colour scheme and wait for the app to follow it. The theme store sets `data-theme` from the
 * media query's change event, which lands after emulateMedia resolves, so a colour read straight away can see the
 * previous theme.
 */
export async function useScheme(page: Page, scheme: 'light' | 'dark') {
	await page.emulateMedia({ colorScheme: scheme, reducedMotion: 'reduce' });
	await expect.poll(() => page.evaluate(() => document.documentElement.getAttribute('data-theme'))).toBe(scheme);
	await settled(page);
}

/** One frame per theme; above 1024 px also with the Workspace panel closed. Returns to light with the panel as it was. */
export async function shoot(page: Page, testInfo: TestInfo, name: string, panel = false) {
	const width = page.viewportSize()!.width;
	await settled(page);
	console.log(`build stamp ${name}: ${await page.locator('.version-overlay').innerText()}`);
	for (const scheme of ['light', 'dark'] as const) {
		await useScheme(page, scheme);
		await page.screenshot({ path: testInfo.outputPath(`${name}-${scheme}.png`) });
	}
	if (panel && width > 1024) {
		const toggle = page.getByRole('button', { name: 'Toggle panel' });
		await toggle.click();
		for (const scheme of ['light', 'dark'] as const) {
			await useScheme(page, scheme);
			await page.screenshot({ path: testInfo.outputPath(`${name}-panelclosed-${scheme}.png`) });
		}
		await toggle.click();
	}
	await useScheme(page, 'light');
}

/**
 * How a comment count sits on its line: nothing is cut, the count stays inside its row, the separator shows only
 * while the count shares the lead's line, and at most one needs-you dot is on the line.
 */
export async function countFit(scope: Locator) {
	const fit = await scope.locator('.count-line').first().evaluate((line) => {
		const box = (element: Element | null) => element?.getBoundingClientRect() ?? null;
		const lead = line.querySelector('.count-lead') as HTMLElement | null;
		const text = line.querySelector('.count-text') as HTMLElement;
		const sep = line.querySelector('.count-sep');
		const holder = (line.closest('li, [data-step-chip]') ?? line) as HTMLElement;
		const lineBox = box(line)!;
		const textBox = box(text)!;
		const holderBox = box(holder)!;
		const leadBox = box(lead);
		const sepBox = box(sep);
		const wrapped = !!leadBox && textBox.top >= leadBox.bottom - 1;
		// The lead's own glyphs, not its box: its padding deliberately sits left of the line, under the clip.
		const leadText = (() => {
			if (!lead) return [];
			const range = document.createRange();
			range.selectNodeContents(lead);
			return [...range.getClientRects()].filter((rect) => rect.width > 0);
		})();
		const ellipsised = [line, ...line.querySelectorAll('*')].some((element) =>
			getComputedStyle(element).textOverflow === 'ellipsis'
			&& (element as HTMLElement).scrollWidth > (element as HTMLElement).clientWidth + 1);
		return {
			lead: lead?.textContent ?? null,
			count: text.textContent,
			inside: textBox.left >= lineBox.left - 1 && textBox.right <= lineBox.right + 1
				&& textBox.left >= holderBox.left - 1 && textBox.right <= holderBox.right + 1,
			// The lead keeps every word: no ellipsis on the line, and none of it clipped at the line's edges.
			leadWhole: !lead || (leadText.length > 0 && !ellipsised && leadText.every((rect) =>
				rect.left >= lineBox.left - 1 && rect.right <= lineBox.right + 1
				&& rect.top >= lineBox.top - 1 && rect.bottom <= lineBox.bottom + 1)),
			wrapped,
			// The separator is seen only while lead and count share a line; a wrapped count's falls left of the edge.
			sepSeen: sepBox ? sepBox.right > lineBox.left + 1 : null,
			dots: line.querySelectorAll('.comment-dot, .dot').length,
		};
	});
	expect(fit.inside, JSON.stringify(fit)).toBe(true);
	expect(fit.leadWhole, JSON.stringify(fit)).toBe(true);
	if (fit.sepSeen !== null) expect(fit.sepSeen, JSON.stringify(fit)).toBe(!fit.wrapped);
	expect(fit.dots, JSON.stringify(fit)).toBeLessThanOrEqual(1);
	return fit;
}

/** The count text's contrast on what is painted behind it, every ancestor's background composited in order. */
export async function countContrast(scope: Locator) {
	return scope.locator('.count-text').first().evaluate((text) => {
		const paint = (colors: string[]) => {
			const canvas = document.createElement('canvas');
			canvas.width = 1;
			canvas.height = 1;
			const context = canvas.getContext('2d', { colorSpace: 'srgb', willReadFrequently: true })!;
			for (const color of colors) { context.fillStyle = color; context.fillRect(0, 0, 1, 1); }
			return [...context.getImageData(0, 0, 1, 1).data.slice(0, 3)];
		};
		const chain: Element[] = [];
		for (let node: Element | null = text; node; node = node.parentElement) chain.unshift(node);
		const behind = paint(['#ffffff', ...chain.map((node) => getComputedStyle(node).backgroundColor)]);
		const ink = paint([`rgb(${behind.join(',')})`, getComputedStyle(text).color]);
		const luminance = (rgb: number[]) => {
			const [r, g, b] = rgb.map((value) => { const c = value / 255; return c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4; });
			return 0.2126 * r + 0.7152 * g + 0.0722 * b;
		};
		const [high, low] = [luminance(ink), luminance(behind)].sort((a, b) => b - a);
		return { ink: `rgb(${ink.join(',')})`, behind: `rgb(${behind.join(',')})`, ratio: Math.round(((high + 0.05) / (low + 0.05)) * 100) / 100, size: getComputedStyle(text).fontSize };
	});
}

/** Contrast in both themes; each must reach 4.5:1, the bar for small text. */
export async function contrastBothThemes(page: Page, scope: Locator, label: string) {
	const result: Record<string, unknown> = {};
	for (const scheme of ['light', 'dark'] as const) {
		await useScheme(page, scheme);
		const measured = await countContrast(scope);
		result[scheme] = measured;
		expect(measured.ratio, `${label} ${scheme} ${JSON.stringify(measured)}`).toBeGreaterThanOrEqual(4.5);
	}
	await useScheme(page, 'light');
	console.log(`CONTRAST ${label}: ${JSON.stringify(result)}`);
}
