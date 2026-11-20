import { expect, type Locator, type Page, type TestInfo } from '@playwright/test';
import { saveEvidenceScreenshot } from './corsia';

// WCAG 2.2 minimum contrast: 1.4.3 for text, 1.4.11 for a graphic that carries meaning.
export const TEXT = 4.5, GRAPHIC = 3;

/**
 * In both themes, expects each target's WCAG contrast ratio to reach its minimum, and keeps one screenshot per theme.
 * The target's colour (its stroke for an SVG) is composited over every background from it up to the first opaque one;
 * a gradient, filter or backdrop filter on the way cannot be composited, so it fails the target instead.
 * With paint 'outline' the target's outline colour is measured over its parent's background, where an offset ring is drawn.
 * With glass, a backdrop-filtered layer (a frosted sheet) is measured over the worst backdrop it can have, pure black and
 * pure white, and the lower ratio counts: the blur's real input cannot be read, so the bound stands for it.
 * The theme's colour transitions and any entrance animation finish first, so a half-way colour is never measured.
 */
export async function expectContrast(page: Page, testInfo: TestInfo, label: string, targets: Record<string, { target: Locator; min: number; paint?: 'outline'; glass?: boolean }>) {
	for (const theme of ['light', 'dark'] as const) {
		await page.emulateMedia({ colorScheme: theme });
		await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
		await page.evaluate(() => Promise.all(document.getAnimations()
			.filter((animation) => animation.effect?.getComputedTiming().iterations !== Infinity)
			.map((animation) => animation.finished.catch(() => undefined))));
		for (const [name, { target, min, paint, glass }] of Object.entries(targets)) {
			const ratio = await target.evaluate((element, { outline, glass }) => {
				// A canvas pixel turns any CSS colour (rgb, srgb, display-p3) into sRGB with alpha.
				const context = document.createElement('canvas').getContext('2d', { willReadFrequently: true })!;
				const rgba = (colour: string) => {
					context.clearRect(0, 0, 1, 1);
					context.fillStyle = colour;
					context.fillRect(0, 0, 1, 1);
					const [r, g, b, a] = context.getImageData(0, 0, 1, 1).data;
					return [r / 255, g / 255, b / 255, a / 255];
				};
				const over = ([r, g, b, a]: number[], under: number[]) => [r * a + under[0] * (1 - a), g * a + under[1] * (1 - a), b * a + under[2] * (1 - a), 1];
				const luminance = (rgb: number[]) => rgb.slice(0, 3).map((c) => (c <= 0.04045 ? c / 12.92 : ((c + 0.055) / 1.055) ** 2.4))
					.reduce((sum, c, i) => sum + c * [0.2126, 0.7152, 0.0722][i], 0);
				const own = getComputedStyle(element);
				const foreground = rgba(outline ? own.outlineColor : element instanceof SVGElement && own.stroke !== 'none' ? own.stroke : own.color);
				const measure = (backstop: number[] | null) => {
					const layers: number[][] = [];
					let opacity = outline ? Number.parseFloat(own.opacity) : 1;
					for (let node: Element | null = outline ? element.parentElement : element; node; node = node.parentElement) {
						const style = getComputedStyle(node);
						const frosted = style.backdropFilter !== 'none' && backstop !== null;
						if (style.backgroundImage !== 'none' || style.filter !== 'none' || (style.backdropFilter !== 'none' && !frosted)) return Number.NaN;
						opacity *= Number.parseFloat(style.opacity);
						const background = rgba(style.backgroundColor);
						background[3] *= Number.parseFloat(style.opacity);
						if (background[3] > 0) layers.unshift(background);
						if (frosted) { layers.unshift(backstop!); break; }
						if (background[3] === 1) break;
					}
					if (layers[0]?.[3] !== 1) return Number.NaN;
					const behind = layers.reduce((under, layer) => over(layer, under));
					const front = over([...foreground.slice(0, 3), foreground[3] * opacity], behind);
					const [light, dark] = [luminance(front), luminance(behind)].sort((a, b) => b - a);
					return (light + 0.05) / (dark + 0.05);
				};
				return glass ? Math.min(measure([0, 0, 0, 1]), measure([1, 1, 1, 1])) : measure(null);
			}, { outline: paint === 'outline', glass: glass === true });
			// One log line per measurement, so the job log carries the actual ratios.
			console.log(`contrast ${testInfo.project.name} ${page.viewportSize()?.width}px ${label} ${theme} ${name}: ${ratio.toFixed(2)} (min ${min})`);
			expect.soft(ratio, `${label}, ${theme} theme: ${name} contrast`).toBeGreaterThanOrEqual(min);
		}
		await saveEvidenceScreenshot(page, testInfo, `${label}-${theme}`, { fullPage: false });
	}
	// Back to the scheme the rest of the flow runs in.
	await page.emulateMedia({ colorScheme: 'light' });
	await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
}
