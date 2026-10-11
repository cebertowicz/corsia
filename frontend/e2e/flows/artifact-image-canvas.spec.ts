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
const KINDS = [
	{ ext: 'png', make: () => syntheticPng(640, 400) },
	{ ext: 'gif', make: () => syntheticGif(320, 200) },
] as const;

// A tab stores no viewer: it opens as a document, and the backend's typed refusal of that text
// open is what makes the canvas show the image.
const CASES = WIDTHS.flatMap((width) => KINDS.map((kind) => ({ width, kind })));

test.describe('@flow:artifact-image @surface:documents image artifacts open as images', () => {
	for (const { width, kind } of CASES) {
	test(`renders a registered ${kind.ext} in the canvas at ${width}px`, async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
		await page.setViewportSize({ width, height: width > 768 ? 1000 : 852 });
		const stamp = Date.now();
		const sessionTitle = `artifact-image-${kind.ext}-${width}-${stamp}`;
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
				({ threadId, artifactId, tabPath, title }) => {
					localStorage.setItem(
						'corsia:artifactWorkspace:v1',
						JSON.stringify({
							threads: {
								[threadId]: {
									tabs: [{
										instanceId: 'tab-image', artifactId, path: tabPath, title,
										loadedRevision: null, latestRevision: null, status: 'loading',
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
				{ threadId: sessionId, artifactId: artifact.id, tabPath: filePath, title: name },
			);

			await loginByUrl(page);
			if (width <= 768) {
				await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			}
			await page.getByText(sessionTitle).first().click();

			const image = page.getByRole('img', { name });
			await expect(image).toBeVisible();
			await expect.poll(() => image.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBeGreaterThan(0);

			// The image tab carries the image glyph, in the strip and in the collapsed rail.
			const imageTab = page.getByRole('tab', { name: new RegExp(name) });
			if (width > 768) await expect(imageTab.locator('.tab-ico circle')).toHaveCount(1);
			if (width > 768 && kind.ext === 'png') {
				await saveEvidenceScreenshot(page, testInfo, 'artifact-image-tab-strip-1440', { fullPage: false });
				await page.getByRole('button', { name: 'Collapse artifact panel' }).click();
				const rail = page.getByRole('button', { name: /Expand artifact panel/ });
				await expect(rail.locator('.rail-ico circle')).toHaveCount(1);
				await saveEvidenceScreenshot(page, testInfo, 'artifact-image-tab-rail-1440', { fullPage: false });
				await rail.click();
				await expect(image).toBeVisible();
			}

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
				await saveEvidenceScreenshot(page, testInfo, `artifact-image-${kind.ext}-${width}-${scheme}`, { fullPage: false });
			}
		} finally {
			fs.rmSync(filePath, { force: true });
		}
	});
	}

	test('opens an uploaded PNG from the 393px artifact list', async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'Sizes are set by the spec itself');
		await page.setViewportSize({ width: 393, height: 852 });
		const stamp = Date.now();
		const sessionTitle = `artifact-upload-${stamp}`;
		const sessionId = await createSession(request, sessionTitle);
		const name = `uploaded-${stamp}.png`;
		const tok = encodeURIComponent(TEST_TOKEN);

		const upload = await request.post(`/api/upload?token=${tok}&thread_id=${sessionId}`, {
			multipart: {
				file: { name, mimeType: 'image/png', buffer: syntheticPng(640, 400) },
			},
		});
		expect(upload.ok(), `upload failed: ${upload.status()} ${await upload.text()}`).toBeTruthy();

		await loginByUrl(page);
		await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.getByText(sessionTitle).first().click();
		await page.getByRole('button', { name: 'Toggle panel' }).click();

		const row = page.getByRole('button', { name: new RegExp(name) });
		await expect(row).toBeVisible();
		await row.click();

		const image = page.getByRole('img', { name });
		await expect(image).toBeVisible();
		await expect.poll(() => image.evaluate((el: HTMLImageElement) => el.naturalWidth)).toBeGreaterThan(0);
		await saveEvidenceScreenshot(page, testInfo, 'artifact-upload-image-393', { fullPage: false });
	});
});
