import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import {
	createSession,
	loginByUrl,
	saveEvidenceScreenshot,
	sidebarThread,
	TEST_TOKEN,
} from '../support/corsia';

function enableMeasurementForSession(sessionId: string): void {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	if (!stateRoot || !python) throw new Error('Artifact desktop proof requires the disposable E2E state and Python');
	execFileSync(python, ['-c', [
		'import sqlite3, sys',
		'connection = sqlite3.connect(sys.argv[1])',
		'connection.execute("UPDATE threads SET agent_enabled = 1, agent_status = \'active\', agent_responsibility = \'Owns artifacts\' WHERE id = ?", (sys.argv[2],))',
		'connection.commit()',
	].join('\n'), `${stateRoot}/corsia.db`, sessionId]);
}

test.describe('@flow:artifact-leaf @surface:chat @surface:documents artifact leaf desktop shell', () => {
	test('renders a thread-pinned split with header controls, resize, collapse, and expand', async ({ page, request }, testInfo) => {
		// This journey captures ten evidence frames and now includes the 900px pane transition.
		test.setTimeout(45_000);
		test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'Slice 1 is desktop-only');

		const stamp = Date.now();
		const sessionTitle = `artifact-leaf-${stamp}`;
		const fileName = `artifact-leaf-${stamp}.md`;
		const sessionId = await createSession(request, sessionTitle);
		const tok = encodeURIComponent(TEST_TOKEN);

		const tree = await (await request.get(`/api/files/tree?token=${tok}`)).json();
		const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
		const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
		expect(workspaceRoot).toEqual(expect.any(String));
		const tabs = Array.from({ length: 8 }, (_, index) => ({
			path: path.join(workspaceRoot, index === 0 ? fileName : `artifact-leaf-${stamp}-${index}.md`),
			title: index === 0 ? fileName : `artifact-leaf-${stamp}-${index}.md`,
		}));
		for (const artifact of tabs) {
			const seed = await request.put(`/api/files/content?token=${tok}`, {
				data: {
					path: artifact.path,
					text: '# Artifact leaf E2E\n\nThread-scoped document body.',
					project_id: null,
					thread_id: sessionId,
					create: true,
				},
			});
			expect(seed.ok(), `seed failed: ${seed.status()} ${await seed.text()}`).toBeTruthy();
		}
		enableMeasurementForSession(sessionId);
		const docPath = tabs[0].path;

		await page.addInitScript(
			({ threadId, artifactTabs }) => {
				localStorage.setItem(
					'corsia:artifactWorkspace:v1',
					JSON.stringify({
						threads: {
							[threadId]: {
								tabs: artifactTabs.map(({ path, title }) => ({
									artifactId: null,
									path,
									title,
									loadedRevision: null,
									status: 'loading',
								})),
								activeTabId: `path:${artifactTabs[0].path}`,
								visible: true,
								activeSurface: 'artifact',
							},
						},
						splitWidth: null,
					}),
				);
			},
			{ threadId: sessionId, artifactTabs: tabs },
		);

		await loginByUrl(page);
		await page.getByText(sessionTitle).first().click();

		const appBox = await page.locator('.app').boundingBox();
		expect(appBox).not.toBeNull();
		expect(appBox!.height).toBe(page.viewportSize()!.height);

		const tab = page.getByRole('tab', { name: fileName });
		const divider = page.getByRole('separator', { name: 'Resize artifact panel' });
		const assertHeaderTracksArtifactWidth = async () => {
			// The split's ResizeObserver follows the pane transition; wait for the
			// actual minimum-width contract instead of sampling its first layout frame.
			await expect.poll(async () => await page.evaluate(() => {
				const leaf = document.querySelector<HTMLElement>('.leaf-col')?.getBoundingClientRect();
				const split = document.querySelector<HTMLElement>('.split')?.getBoundingClientRect();
				if (!leaf || !split) throw new Error('split width is unavailable');
				return split.width - leaf.width - 8;
			})).toBeGreaterThanOrEqual(420);
			const geometry = await page.evaluate(() => {
				const title = document.querySelector<HTMLElement>('.h-title')?.getBoundingClientRect();
				const divider = document.querySelector<HTMLElement>('[aria-label="Resize artifact panel"]')?.getBoundingClientRect();
				const leaf = document.querySelector<HTMLElement>('.leaf-col')?.getBoundingClientRect();
				const split = document.querySelector<HTMLElement>('.split')?.getBoundingClientRect();
				const main = document.querySelector<HTMLElement>('.main');
				if (!title || !divider || !leaf || !split || !main) throw new Error('split header geometry is unavailable');
				return {
					headerInset: Number.parseFloat(getComputedStyle(main).getPropertyValue('--artifact-header-inset')),
					titleRight: title.right,
					dividerLeft: divider.left,
					splitWidth: split.width,
					artifactWidth: leaf.width,
					occupiedWidth: leaf.width + divider.width,
				};
			});
			expect(geometry.headerInset).toBeCloseTo(geometry.occupiedWidth, 0);
			expect(geometry.titleRight).toBeLessThanOrEqual(geometry.dividerLeft);
			// This test keeps the plan pane open. The available width must preserve the
			// 420px chat floor instead of forcing the nominal 60% artifact width.
			expect(geometry.splitWidth - geometry.artifactWidth - 8).toBeGreaterThanOrEqual(420);
		};
		await expect(tab).toBeVisible();
		await expect(page.getByText('Artifact leaf E2E')).toBeVisible();
		await expect(divider).toBeVisible();
		await assertHeaderTracksArtifactWidth();

		await page.setViewportSize({ width: 1440, height: 900 });
		const measurement = page.getByRole('button', { name: `Measurement for ${sessionTitle}`, exact: true });
		const settings = page.getByRole('button', { name: 'Thread settings', exact: true });
		const panelToggle = page.getByRole('button', { name: 'Toggle panel', exact: true });
		const strip = page.locator('.canvas-header-strip');
		const drawer = page.locator('aside.pane');
		// The strip ends before the first control, and each control takes its own pointer
		// events (a trial click fails when the strip or an overlaid drawer would take them).
		const assertControlsReachable = async () => {
			await expect.poll(async () => {
				const [stripBox, measurementBox] = await Promise.all([strip.boundingBox(), measurement.boundingBox()]);
				return stripBox!.x + stripBox!.width <= measurementBox!.x;
			}).toBe(true);
			for (const control of [measurement, settings, panelToggle]) await control.click({ trial: true });
		};

		await expect(drawer).not.toHaveClass(/(^|\s)(overlay|collapsed)(\s|$)/);
		await assertControlsReachable();
		await saveEvidenceScreenshot(page, testInfo, 'header-icons-light-1440-drawer-inline', { fullPage: false });
		await measurement.click();
		await expect(page.getByRole('button', { name: 'Close Measurement' })).toBeVisible();
		await page.getByRole('button', { name: 'Close Measurement' }).click();
		await expect(tab).toBeVisible();
		await settings.click();
		await expect(page.getByText('Context files', { exact: true })).toBeVisible();
		await page.getByRole('button', { name: `Agent: ${sessionTitle}` }).click();
		await expect(tab).toBeVisible();

		await page.getByRole('tablist', { name: 'Open side panel items' }).evaluate((element) => {
			element.scrollLeft = element.scrollWidth;
		});
		const lastTab = page.locator('.canvas-header-strip .tab').last();
		const [lastTabBox, measurementBox] = await Promise.all([lastTab.boundingBox(), measurement.boundingBox()]);
		expect(lastTabBox!.x + lastTabBox!.width).toBeLessThanOrEqual(measurementBox!.x);
		await lastTab.click();
		await expect(lastTab).toHaveAttribute('aria-selected', 'true');

		const collapseButton = page.getByRole('button', { name: 'Collapse artifact panel' });
		const [headerBox, tabBox, collapseBox] = await Promise.all([
			page.locator('.shell-header').boundingBox(),
			tab.boundingBox(),
			collapseButton.boundingBox(),
		]);
		expect(headerBox).not.toBeNull();
		expect(tabBox).not.toBeNull();
		expect(collapseBox).not.toBeNull();
		expect(tabBox!.y).toBeGreaterThanOrEqual(headerBox!.y);
		expect(tabBox!.y + tabBox!.height).toBeLessThanOrEqual(headerBox!.y + headerBox!.height);
		expect(collapseBox!.y).toBeGreaterThanOrEqual(headerBox!.y);
		expect(collapseBox!.y + collapseBox!.height).toBeLessThanOrEqual(headerBox!.y + headerBox!.height);

		await panelToggle.click();
		await expect(drawer).toHaveClass(/(^|\s)collapsed(\s|$)/);
		await assertControlsReachable();
		await saveEvidenceScreenshot(page, testInfo, 'header-icons-light-1440-drawer-collapsed', { fullPage: false });

		// The drawer overlays the canvas only once inline it would leave less than the combined
		// minimum; with the desktop sidebar open that first happens below 1440.
		await page.setViewportSize({ width: 1300, height: 900 });
		await panelToggle.click();
		await expect(drawer).toHaveClass(/(^|\s)overlay(\s|$)/);
		await assertControlsReachable();
		const [panelBox, drawerBox] = await Promise.all([panelToggle.boundingBox(), drawer.boundingBox()]);
		expect(panelBox!.x + panelBox!.width).toBeLessThanOrEqual(drawerBox!.x);
		await saveEvidenceScreenshot(page, testInfo, 'header-icons-light-1300-drawer-overlaid', { fullPage: false });
		await panelToggle.click();
		await expect(drawer).toHaveClass(/(^|\s)collapsed(\s|$)/);

		await page.setViewportSize({ width: 1440, height: 1000 });
		await panelToggle.click();
		await expect(drawer).not.toHaveClass(/(^|\s)(overlay|collapsed)(\s|$)/);

		await divider.focus();
		await page.keyboard.press('Home');
		await expect.poll(async () => Number(await divider.getAttribute('aria-valuenow'))).toBe(360);
		await assertHeaderTracksArtifactWidth();
		await page.keyboard.press('End');
		await expect.poll(async () => Number(await divider.getAttribute('aria-valuenow'))).toBeGreaterThan(360);
		await assertHeaderTracksArtifactWidth();
		await saveEvidenceScreenshot(page, testInfo, 'artifact-leaf-desktop-split', { fullPage: false });

		await collapseButton.click();
		const rail = page.getByRole('button', { name: `Expand artifact panel (${tabs.length} open)` });
		await expect(rail).toBeVisible();
		await expect(page.getByRole('textbox', { name: 'Message CORSIA' })).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'artifact-leaf-desktop-rail', { fullPage: false });

		await rail.click();
		await expect(tab).toBeVisible();
		await expect(divider).toBeVisible();
	});

	test('ends an ordinary chat canvas strip at its two visible controls', async ({ page, request }, testInfo) => {
		test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'Desktop-only control geometry');

		const stamp = Date.now();
		const sessionTitle = `ordinary-artifact-${stamp}`;
		const fileName = `ordinary-artifact-${stamp}.md`;
		const sessionId = await createSession(request, sessionTitle);
		const tok = encodeURIComponent(TEST_TOKEN);
		const tree = await (await request.get(`/api/files/tree?token=${tok}`)).json();
		const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
		const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
		expect(workspaceRoot).toEqual(expect.any(String));
		const artifactPath = path.join(workspaceRoot, fileName);
		const seed = await request.put(`/api/files/content?token=${tok}`, {
			data: { path: artifactPath, text: '# Ordinary artifact\n\nCanvas evidence.', project_id: null, thread_id: sessionId, create: true },
		});
		expect(seed.ok(), `seed failed: ${seed.status()} ${await seed.text()}`).toBeTruthy();
		await page.addInitScript(
			({ threadId, openPath, title }) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
				threads: { [threadId]: { tabs: [{ artifactId: null, path: openPath, title, loadedRevision: null, status: 'loading' }], activeTabId: `path:${openPath}`, visible: true, activeSurface: 'artifact' } },
				splitWidth: null,
			})),
			{ threadId: sessionId, openPath: artifactPath, title: fileName },
		);
		await loginByUrl(page);
		await sidebarThread(page, sessionTitle).click();
		await page.setViewportSize({ width: 1440, height: 900 });
		await expect(page.getByRole('tab', { name: fileName })).toBeVisible();
		await expect(page.getByRole('button', { name: 'Thread settings', exact: true })).toBeVisible();
		await expect(page.getByRole('button', { name: 'Toggle panel', exact: true })).toBeVisible();
		expect(await page.getByRole('button', { name: `Measurement for ${sessionTitle}`, exact: true }).count()).toBe(0);
		await expect.poll(async () => await page.evaluate(() => {
			const strip = document.querySelector<HTMLElement>('.canvas-header-strip')?.getBoundingClientRect();
			const controls = document.querySelector<HTMLElement>('.h-right')?.getBoundingClientRect();
			if (!strip || !controls) throw new Error('ordinary canvas control geometry is unavailable');
			return strip.right <= controls.left;
		})).toBe(true);
		for (const theme of ['dark', 'light'] as const) {
			await page.locator('html').evaluate((element, value) => element.setAttribute('data-theme', value), theme);
			await saveEvidenceScreenshot(page, testInfo, `header-icons-after-${theme}-1440-ordinary-chat-canvas`, { fullPage: false });
		}
	});
});
