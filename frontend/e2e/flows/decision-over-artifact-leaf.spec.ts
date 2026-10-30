import path from 'node:path';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { createSession, openAppByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';

const decision = {
	id: 'split-decision', plan_id: 'plan-1', node_id: 'node-1', source_node_id: 'node-1',
	thread_id: 'child', title: 'Choose an audit access boundary',
	context: 'An agent needs to inspect the audit evidence before acting.',
	tradeoff: 'The access boundary changes which evidence is available.',
	recommendation: 'A', rationale: 'Keep sensitive material governed.',
	options: [
		{ id: 'A', label: 'Scoped diagnostics', detail: 'Use only permitted evidence.' },
		{ id: 'B', label: 'Read-only logs', detail: 'Review the raw operational log.' },
	],
	other_enabled: true, order_key: 0, revision: 1,
	raised_at: '2026-09-23T10:00:00Z',
	source: { thread_id: 'child', thread_label: 'Worker', project_id: 'project-1', project_label: 'CORSIA', archived: false, parent_reviewed: false },
};

const queued = { external_id: decision.id, renderer: 'decision', detail: decision };

test('@surface:attention decision drawer stays above an open artifact leaf', async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop');
	await page.setViewportSize({ width: 2000, height: 1060 });
	await page.route('**/api/attention/queue', (route) => route.fulfill({
		status: 200, contentType: 'application/json',
		body: JSON.stringify({ current: queued, count: 1, items: [queued] }),
	}));
	const title = `split-decision-${Date.now()}`;
	const threadId = await createSession(request, title);
	const tree = await request.get(`/api/files/tree?token=${TEST_TOKEN}`);
	expect(tree.ok()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await tree.json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const docPath = path.join(workspace!, `${title}.md`);
	const seeded = await request.put(`/api/files/content?token=${TEST_TOKEN}`, {
		data: { path: docPath, text: '# Audit evidence\n\nA real document is open behind this decision.', thread_id: threadId, create: true },
	});
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId = (await seeded.json()).artifact.id;
	const comment = await request.post(`/api/artifacts/${artifactId}/comments?token=${TEST_TOKEN}`, {
		data: { body: 'Keep this decision visible over the document.', anchor_text: 'real document', from_off: 27, to_off: 40, delivery_policy: 'immediate' },
	});
	expect(comment.ok(), await comment.text()).toBeTruthy();
	await page.addInitScript(({ threadId, artifactId, docPath }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: {
				tabs: [{ kind: 'document', instanceId: 'test-leaf', artifactId, path: docPath,
					title: 'Audit evidence', threadId: null, loadedRevision: null,
					latestRevision: null, status: 'loading' }],
				activeTabId: 'test-leaf', visible: true, activeSurface: 'artifact',
			} }, splitWidth: 774,
		}));
	}, { threadId, artifactId, docPath });
	await openAppByUrl(page);
	await page.locator('.thread-item').filter({ hasText: title }).click();
	await expect(page.locator('.leaf-col')).toBeVisible();
	const divider = page.getByRole('separator', { name: 'Resize Files panel' });
	await expect(divider).toBeVisible();
	const leftSidebar = page.locator('aside.sidebar:not(.collapsed)');
	await expect(leftSidebar).toBeVisible();
	const rightPane = page.locator('aside.pane');
	await expect(rightPane).toBeVisible();
	await page.locator('[data-attention-dock] .dock-bar').click();
	const drawer = page.getByRole('dialog', { name: 'Decision — Choose an audit access boundary' });
	await expect(drawer).toBeVisible();
	const panel = drawer;
	// Engine-independent guard: a backdrop mounted under the app shell can be
	// clipped or sunk by that shell in a WebKit webview even where Chromium passes.
	expect(
		await page.evaluate(() => document.querySelector('[data-adaptive-drawer]')?.parentElement === document.body),
		'drawer backdrop is mounted on document.body, outside every app-shell ancestor',
	).toBe(true);
	const panelBox = (await panel.boundingBox())!;
	const dividerBox = (await divider.boundingBox())!;
	expect(panelBox.x).toBeLessThan(dividerBox.x + 80);
	expect(panelBox.x + panelBox.width).toBeGreaterThan(dividerBox.x + 80);
	const leftSidebarBox = (await leftSidebar.boundingBox())!;
	const rightPaneBox = (await rightPane.boundingBox())!;
	const layerHits = await page.evaluate(([sidebarX, leafX, rightX, panelX, panelY]) => ({
		leftSidebar: !!document.elementFromPoint(sidebarX, 300)?.closest('[data-adaptive-drawer]'),
		leaf: !!document.elementFromPoint(leafX, panelY)?.closest('.drawer-panel'),
		rightPane: !!document.elementFromPoint(rightX, 300)?.closest('[data-adaptive-drawer]'),
		edges: [20, window.innerWidth - 20].map((x) => !!document.elementFromPoint(x, 300)?.closest('[data-adaptive-drawer]')),
	}), [leftSidebarBox.x + leftSidebarBox.width / 2, dividerBox.x + 80, rightPaneBox.x + rightPaneBox.width / 2, panelBox.x + panelBox.width / 2, panelBox.y + panelBox.height / 2]);
	expect(layerHits.leftSidebar, 'scrim covers the expanded left sidebar').toBe(true);
	expect(layerHits.leaf, 'drawer wins hit testing over the artifact leaf').toBe(true);
	expect(layerHits.rightPane, 'scrim covers the right plan pane').toBe(true);
	expect(layerHits.edges, 'scrim covers the left and right viewport edges').toEqual([true, true]);
	await expect(drawer.getByRole('button', { name: /Read-only logs/ })).toBeInViewport();
	await saveEvidenceScreenshot(page, testInfo, 'decision-over-open-artifact-leaf', { fullPage: false });

	await page.setViewportSize({ width: 1280, height: 800 });
	const sidebar = page.locator('aside.pane.overlay');
	await expect(sidebar).toBeVisible();
	const narrowBox = (await panel.boundingBox())!;
	const sidebarBox = (await sidebar.boundingBox())!;
	expect(narrowBox.x + narrowBox.width - 24).toBeGreaterThan(sidebarBox.x);
	const trailingHit = await page.evaluate(([x, y]) =>
		!!document.elementFromPoint(x, y)?.closest('.drawer-panel'),
		[narrowBox.x + narrowBox.width - 24, narrowBox.y + narrowBox.height / 2]);
	expect(trailingHit, 'drawer stays above the right sidebar').toBe(true);
	await saveEvidenceScreenshot(page, testInfo, 'decision-over-artifact-and-sidebar', { fullPage: false });

	await page.setViewportSize({ width: 393, height: 852 });
	const mobileBox = (await panel.boundingBox())!;
	expect(mobileBox.x).toBeGreaterThanOrEqual(0);
	expect(mobileBox.y).toBeGreaterThanOrEqual(0);
	expect(mobileBox.x + mobileBox.width).toBeLessThanOrEqual(393);
	expect(mobileBox.y + mobileBox.height).toBeLessThanOrEqual(852);
	const mobileHits = await page.evaluate(([panelX, panelY]) => ({
		panel: !!document.elementFromPoint(panelX, panelY)?.closest('.drawer-panel'),
		edges: [20, window.innerWidth - 20].map((x) => !!document.elementFromPoint(x, 300)?.closest('[data-adaptive-drawer]')),
	}), [mobileBox.x + mobileBox.width / 2, mobileBox.y + mobileBox.height / 2]);
	expect(mobileHits.panel, 'drawer remains topmost at its centre').toBe(true);
	expect(mobileHits.edges, 'mobile scrim covers both viewport edges').toEqual([true, true]);
	await saveEvidenceScreenshot(page, testInfo, 'decision-over-artifact-and-phone', { fullPage: false });
	await page.setViewportSize({ width: 1280, height: 800 });
	await drawer.getByRole('button', { name: 'Done' }).click();
	await expect(page.locator('.leaf-col')).toBeVisible();
});
