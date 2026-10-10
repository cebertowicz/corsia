import fs from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import {
	createSession,
	loginByUrl,
	saveEvidenceScreenshot,
	TEST_TOKEN,
} from '../support/corsia';
import { syntheticGif, syntheticPng } from '../support/syntheticImages';

const WIDTHS = [1440, 393];
// A tab saved before the backend's type was known carries no `isImage`; the canvas must still
// open it as an image, through the typed refusal of a text open.
const TAB_FACTS = [
	{ label: 'a tab the backend typed', isImage: true },
	{ label: 'a tab saved before typing', isImage: false },
] as const;
const KINDS = [
	{ ext: 'png', make: () => syntheticPng(640, 400) },
	{ ext: 'gif', make: () => syntheticGif(320, 200) },
] as const;

// The legacy tab is one representative case, not another full matrix.
const CASES = WIDTHS.flatMap((width) => KINDS.flatMap((kind) => TAB_FACTS
	.filter((fact) => fact.isImage || (kind.ext === 'png' && width === 1440))
	.map((fact) => ({ width, kind, fact }))));

test.describe('@flow:artifact-image @surface:documents image artifacts open as images', () => {
	for (const { width, kind, fact } of CASES) {
	test(`renders a registered ${kind.ext} in the canvas at ${width}px from ${fact.label}`, async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
		await page.setViewportSize({ width, height: width > 768 ? 1000 : 852 });
		const stamp = Date.now();
		const sessionTitle = `artifact-image-${kind.ext}-${width}-${fact.isImage ? 'typed' : 'legacy'}-${stamp}`;
		const sessionId = await createSession(request, sessionTitle);
		const tok = encodeURIComponent(TEST_TOKEN);

		const tree = await (await request.get(`/api/files/tree?token=${tok}`)).json();
		const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
		const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
		expect(workspaceRoot).toEqual(expect.any(String));

		const name = `synthetic-${stamp}.${kind.ext}`;
		const filePath = path.join(workspaceRoot, name);
		fs.writeFileSync(filePath, kind.make());
		try {
			const registered = await request.post(`/api/files/register?token=${tok}`, {
				data: { path: filePath, thread_id: sessionId },
			});
			expect(registered.ok(), `register failed: ${registered.status()}`).toBeTruthy();
			const artifact = (await registered.json()).artifact;
			expect(artifact.type).toBe('image');

			await page.addInitScript(
				({ threadId, artifactId, tabPath, title, isImage }) => {
					localStorage.setItem(
						'corsia:artifactWorkspace:v1',
						JSON.stringify({
							threads: {
								[threadId]: {
									tabs: [{
										instanceId: 'tab-image', artifactId, path: tabPath, title,
										loadedRevision: null, latestRevision: null, status: 'loading',
										...(isImage ? { isImage: true } : {}),
									}],
									activeTabId: 'tab-image',
									visible: true,
									activeSurface: 'artifact',
								},
							},
							splitWidth: null,
						}),
					);
				},
				{ threadId: sessionId, artifactId: artifact.id, tabPath: filePath, title: name, isImage: fact.isImage },
			);

			await loginByUrl(page);
			if (width <= 768) {
				await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			}
			await page.getByText(sessionTitle).first().click();

			const image = page.getByRole('img', { name });
			await expect(image).toBeVisible();
			await expect.poll(() => image.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBeGreaterThan(0);

			// Fits the canvas and is never drawn larger than the file itself.
			const natural = await image.evaluate((el: HTMLImageElement) => el.naturalWidth);
			const box = (await image.boundingBox())!;
			expect(box.width).toBeLessThanOrEqual(natural + 0.5);
			expect(box.x + box.width).toBeLessThanOrEqual(width + 0.5);
			expect(await page.evaluate(() => document.documentElement.scrollWidth)).toBeLessThanOrEqual(width);

			for (const scheme of ['dark', 'light'] as const) {
				await page.emulateMedia({ colorScheme: scheme });
				await expect(page.locator('html')).toHaveAttribute('data-theme', scheme);
				await expect(image).toBeVisible();
				await saveEvidenceScreenshot(page, testInfo, `artifact-image-${kind.ext}-${width}-${fact.isImage ? 'typed' : 'legacy'}-${scheme}`, { fullPage: false });
			}
		} finally {
			fs.rmSync(filePath, { force: true });
		}
	});
	}
});
