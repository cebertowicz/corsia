import { test, expect } from '../support/test';
import { openLiveDocument, seedLiveDocument } from '../support/liveDocument';
import { detailsEntry, documentBarHost, expectOneRow } from '../support/documentBar';

// Reload shows only on a snapshot document (a live session never offers it). While the canvas is compact (every width here)
// it is the reload mark right after the document's name in the app bar; History and the rest are in Details. Its container queries need browser layout, so the status component is mounted
// inside the real canvas, as quiet-save-cue.spec.ts does, with the controls a recorded document carries.
for (const width of [320, 375, 393, 480, 600]) {
	test(`@surface:doc-header File changed · Reload stays inside the bar at ${width}`, async ({ page, request }, info) => {
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
		const host = documentBarHost(page);
		const reload = host.getByRole('button', { name: 'File changed · Reload' });
		await expect(reload).toBeVisible();
		// One File changed only: the bar's mark, not a second row in Details.
		await expect(page.getByRole('button', { name: 'File changed · Reload', includeHidden: true })).toHaveCount(1);
		const [inside, chip] = [await host.boundingBox(), await reload.boundingBox()];
		expect(chip!.x).toBeGreaterThanOrEqual(Math.max(0, inside!.x) - 0.5);
		expect(chip!.x + chip!.width).toBeLessThanOrEqual(Math.min(width, inside!.x + inside!.width) + 0.5);
		await expectOneRow(page);
		// The mounted header is the last one (the real one is hidden beside it), so its panel is read from it.
		await detailsEntry(page).click();
		await expect(detailsEntry(page)).toHaveAttribute('aria-expanded', 'true');
		const panel = page.locator('.document-header').last().locator('.doc-panel');
		await expect(panel).toBeVisible();
		const history = panel.getByRole('button', { name: 'History', exact: true });
		await expect(history).toBeVisible();
		const [bounds, entry] = [await panel.boundingBox(), await history.boundingBox()];
		expect(entry!.x).toBeGreaterThanOrEqual(Math.max(0, bounds!.x) - 0.5);
		expect(entry!.x + entry!.width).toBeLessThanOrEqual(Math.min(width, bounds!.x + bounds!.width) + 0.5);
		if (process.env.VH_OUT) await page.screenshot({ path: `${process.env.VH_OUT}/stale-reload-${width}.png`, animations: 'disabled' });
	});
}
