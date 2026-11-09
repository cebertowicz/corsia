import { test } from '../support/test';
import { expect } from '@playwright/test';
import { bar, expectBarFits, openDocument, openImage, seedImageDocument } from '../support/docImageComments';

for (const width of [375, 393]) for (const colorScheme of ['light', 'dark'] as const) {
	test(`@flow:doc-image-toolbar-phone @surface:documents image Send stays in one row (${width}, ${colorScheme})`, async ({ page, request }, info) => {
		test.skip(!['chromium-desktop', 'webkit-desktop'].includes(info.project.name), 'Viewport is set by this spec');
		test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
		test.setTimeout(90_000);
		await page.setViewportSize({ width, height: 852 });
		await page.emulateMedia({ colorScheme });
		const seeded = await seedImageDocument(page, request, 'image-toolbar', null, '# Image review\n\n![Mock screen](shots/screen.png)\n');
		await openDocument(page, seeded, width);
		await expect(page.locator('html')).toHaveAttribute('data-theme', colorScheme);
		await openImage(page);
		const toolbar = bar(page);
		const image = page.locator('.image-overlay .frame img');
		const before = (await toolbar.boundingBox())!;
		const imageTop = (await image.boundingBox())!.y;
		await toolbar.screenshot({ path: info.outputPath('bar-before.png') });
		await toolbar.locator('[data-image-comment]').click();
		const region = (await page.locator('.image-overlay [data-image-regions]').boundingBox())!;
		await page.mouse.click(region.x + region.width * 0.3, region.y + region.height * 0.4);
		const composer = page.getByPlaceholder('Comment, or @ to tag an agent');
		await expect(composer).toBeVisible();
		await composer.fill('A comment with forty ordinary characters.');
		await composer.press('ControlOrMeta+Enter');
		await expect(composer).toHaveCount(0);
		await expect(toolbar.getByRole('button', { name: /^Send/ })).toBeVisible();
		await expectBarFits(page);
		const after = (await toolbar.boundingBox())!;
		await toolbar.screenshot({ path: info.outputPath('bar-posted.png') });
		expect(after.height, 'posting keeps the toolbar in its original row').toBe(before.height);
		expect((await image.boundingBox())!.y, 'the fitted image stays in place').toBe(imageTop);
		const controls = await toolbar.locator('button').evaluateAll(buttons => buttons.map(button => {
			const rect = button.getBoundingClientRect();
			return { name: button.getAttribute('aria-label') ?? button.textContent?.trim(), left: rect.left, right: rect.right, centre: rect.top + rect.height / 2 };
		}));
		expect(controls).toHaveLength(5);
		for (const control of controls) {
			expect(control.left, `${control.name} starts inside the bar`).toBeGreaterThanOrEqual(after.x);
			expect(control.right, `${control.name} ends inside the bar`).toBeLessThanOrEqual(after.x + after.width);
			expect(Math.abs(control.centre - controls[0].centre), `${control.name} shares the row`).toBeLessThanOrEqual(1);
		}
		await info.attach('toolbar-edges', { body: JSON.stringify({ width, colorScheme, bar: after, controls }), contentType: 'application/json' });
		const reply = page.getByPlaceholder('Reply…');
		await reply.fill('A reply with forty ordinary characters.');
		await reply.press('ControlOrMeta+Enter');
		await expect(reply).toHaveValue('');
		await expectBarFits(page);
		expect((await toolbar.boundingBox())!.height).toBe(before.height);
		expect((await image.boundingBox())!.y).toBe(imageTop);
	});
}
