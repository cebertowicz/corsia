import { test, expect } from '../support/test';
import { openLiveDocument, seedLiveDocument } from '../support/liveDocument';

// Reload shows only on a snapshot document (a live session never offers it), beside the full tool row: comments,
// delivery, review and History. Its container queries need browser layout, so the status component is mounted
// inside the real canvas, as quiet-save-cue.spec.ts does, with the controls a recorded document carries.
for (const width of [320, 375, 393, 480, 600]) {
	test(`@surface:doc-header File changed · Reload stays inside the header at ${width}`, async ({ page, request }, info) => {
		test.skip(info.project.name.endsWith('-mobile'), 'The widths are set here; one project is enough.');
		await page.setViewportSize({ width, height: 852 });
		const fixture = await seedLiveDocument(request, `stale-fit-${width}`, '# Changed outside\n\nA document whose file changed.\n');
		await openLiveDocument(page, fixture);
		await page.evaluate(async (artifactId) => {
			const svelte = '/node_modules/.vite/deps/svelte.js';
			const component = '/src/lib/components/DocViewerStatus.svelte';
			const { mount } = await import(/* @vite-ignore */ svelte);
			const { default: Status } = await import(/* @vite-ignore */ component);
			const original = document.querySelector<HTMLElement>('.document-header')!;
			original.style.display = 'none';
			const target = document.createElement('div'); original.after(target);
			mount(Status, { target, props: { ready: true, stale: true, readonly: false, saveStatus: 'idle', commentCount: 3,
				orphanedCount: 0, artifactId, targetInstanceId: 'tab-stale', freshness: null, onOpenComments: () => {},
				historyAvailable: true, onOpenHistory: () => {}, onReload: () => {} } });
		}, fixture.artifactId);
		const reload = page.getByRole('button', { name: 'File changed · Reload' });
		await expect(reload).toBeVisible();
		const header = page.locator('.document-header').last();
		const [inside, chip] = [await header.boundingBox(), await reload.boundingBox()];
		expect(chip!.x).toBeGreaterThanOrEqual(Math.max(0, inside!.x) - 0.5);
		expect(chip!.x + chip!.width).toBeLessThanOrEqual(Math.min(width, inside!.x + inside!.width) + 0.5);
		await expect(page.getByRole('button', { name: 'History', exact: true })).toBeVisible();
		if (process.env.VH_OUT) await page.screenshot({ path: `${process.env.VH_OUT}/stale-reload-${width}.png`, animations: 'disabled' });
	});
}
