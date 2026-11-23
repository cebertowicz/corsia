import { test, expect } from '../support/test';
import { openLiveDocument, seedLiveDocument } from '../support/liveDocument';

for (const width of [393, 1440]) {
	test(`Comments loading remains named and visible at ${width}`, async ({ page, request }, info) => {
		test.skip(info.project.name.endsWith('-mobile'), 'The viewport is set here.');
		await page.setViewportSize({ width, height: 852 });
		const fixture = await seedLiveDocument(request, `comments-loading-${width}`, '# Launch plan\n\nA clear plan for the next release.\n');
		await openLiveDocument(page, fixture);
		await page.evaluate(async (artifactId) => {
			const svelte = '/node_modules/.vite/deps/svelte.js';
			const component = '/src/lib/components/DocViewerStatus.svelte';
			const { mount } = await import(/* @vite-ignore */ svelte);
			const { default: Status } = await import(/* @vite-ignore */ component);
			const original = document.querySelector<HTMLElement>('.document-header')!;
			original.style.display = 'none';
			const target = document.createElement('div'); original.after(target);
			mount(Status, { target, props: { ready: true, stale: false, readonly: false, commentsLoading: true,
				commentCount: 0, artifactId, targetInstanceId: 'tab-loading', freshness: null, onOpenComments: () => {} } });
		}, fixture.artifactId);
		const button = page.getByRole('button', { name: 'Loading comments', exact: true });
		await expect(button).toHaveCount(1);
		await expect(button).toBeVisible();
		await expect(button).toBeDisabled();
		await expect(button).toHaveAttribute('aria-busy', 'true');
		await expect(button.locator('.spinner[aria-hidden="true"]')).toBeVisible();
		for (const theme of ['light', 'dark'] as const) {
			await page.emulateMedia({ colorScheme: theme });
			await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
			if (process.env.CORSIA_FRAMES_DIR) await page.screenshot({ path: `${process.env.CORSIA_FRAMES_DIR}/comments-loading-${width}-${theme}.png`, animations: 'disabled' });
		}
	});
}
