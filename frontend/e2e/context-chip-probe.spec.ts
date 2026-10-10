import { expect, test } from '@playwright/test';

type Theme = 'dark' | 'light';

async function visitChip(
	page: import('@playwright/test').Page,
	theme: Theme,
	source: 'measured' | 'estimated',
	pct = 7,
	open = true,
	width = 393,
	model: 'short' | 'long' = 'short',
) {
	await page.setViewportSize({ width, height: 240 });
	await page.emulateMedia({ colorScheme: theme });
	await page.goto(`/_design/context-chip-probe?source=${source}&pct=${pct}&theme=${theme}&model=${model}`, { waitUntil: 'domcontentloaded' });
	const chip = page.locator('button.context-chip');
	if (open) {
		await chip.click();
		await expect(chip).toHaveClass(/\bopen\b/);
	} else {
		await expect(chip).not.toHaveClass(/\bopen\b/);
	}
	return chip;
}

test('reserves the estimate marker without changing width at 393px', async ({ page }) => {
	for (const theme of ['dark', 'light'] as const) {
		const measured = await visitChip(page, theme, 'measured');
		const measuredWidth = await measured.evaluate((element) => element.getBoundingClientRect().width);
		await expect(measured.locator('.ctx-pct')).toHaveText('~7%');
		await expect(measured.locator('.ctx-estimate-marker')).toBeHidden();

		const estimated = await visitChip(page, theme, 'estimated');
		await expect(estimated.locator('.ctx-pct')).toHaveText('~7%');
		await expect(estimated.locator('.ctx-estimate-marker')).toBeVisible();
		const estimatedWidth = await estimated.evaluate((element) => element.getBoundingClientRect().width);
		expect(estimatedWidth, `${theme} 393px`).toBe(measuredWidth);

		for (const source of ['measured', 'estimated'] as const) {
			const chip = await visitChip(page, theme, source, 7, true, 412, 'short');
			const geometry = await chip.evaluate((element) => {
				const chip = element.getBoundingClientRect();
				const label = element.querySelector('.ctx-pct')?.getBoundingClientRect();
				return Boolean(label
					&& label.left >= chip.left && label.right <= chip.right
					&& label.top >= chip.top && label.bottom <= chip.bottom);
			});
			expect(geometry, `${theme}/${source} 412px`).toBe(true);
		}
	}
});

test('keeps label and estimate-marker colors inherited from the context chip', async ({ page }) => {
	for (const theme of ['dark', 'light'] as const) for (const source of ['measured', 'estimated'] as const) {
		for (const [pct, open] of [[7, false], [7, true], [75, false], [75, true], [90, false], [90, true]] as const) {
			const chip = await visitChip(page, theme, source, pct, open);
			const colors = await chip.evaluate((element) => {
				const label = element.querySelector<HTMLElement>('.ctx-pct');
				const marker = element.querySelector<HTMLElement>('.ctx-estimate-marker');
				return {
					chip: getComputedStyle(element).color,
					label: label && getComputedStyle(label).color,
					marker: marker && getComputedStyle(marker).color,
				};
			});
			const state = `${theme}/${source}/${pct}/${open ? 'open' : 'closed'}`;
			expect(colors.label, state).toBe(colors.chip);
			expect(colors.marker, state).toBe(colors.label);
		}
	}
});

test('preserves the #2965 priority result at 320px and 360px', async ({ page }) => {
	for (const width of [320, 360]) {
		const result = async (variant: 'base' | 'candidate') => {
			await page.setViewportSize({ width, height: 240 });
			await page.goto(`/_design/context-chip-probe?variant=${variant}&source=estimated&pct=7`, { waitUntil: 'domcontentloaded' });
			return page.locator('.action-row').evaluate((row) => {
				const visible = (selector: string) => {
					const element = row.querySelector<HTMLElement>(selector);
					const rect = element?.getBoundingClientRect();
					return Boolean(rect && rect.width > 0 && rect.height > 0);
				};
				return { model: visible('.model-chip'), effort: visible('.effort-chip'), context: visible('.context-chip') };
			});
		};
		expect(await result('candidate'), `${width}px priority`).toEqual(await result('base'));
	}
});
