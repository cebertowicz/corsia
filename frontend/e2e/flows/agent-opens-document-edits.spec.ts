import { expect, test } from '@playwright/test';
import {
	canvasState,
	expectCannotShowLink,
	expectCapability,
	openThreadView,
	postLinks,
	queueOpen,
	releaseOpen,
	revealEarlierCompletedWork,
	revealWorked,
	serverText,
	thenGate,
	typeAtEnd,
	newSocketLog,
} from '../support/documentOpen';
import { holdDocumentSaves } from '../support/documentOpenHolds';

// An edited document is the case where replacing the view would cost the user
// most. Saves are held at the network, so each request provably arrives while
// the edit exists only in the editor; the server text is checked, not assumed.

for (const [width, label, project] of [[1440, 'desktop', 'chromium-desktop'], [393, 'compact', 'chromium-mobile']] as const) {
	test(`@surface:agent-opens-document ${label} keeps an unsaved visible document through different and same requests`, async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(120_000);
		await page.setViewportSize({ width, height: 1000 });
		const log = newSocketLog();
		const saves = await holdDocumentSaves(page, log);
		const { threadId, a, b } = await openThreadView(page, request, log, `${label}-unsaved`);
		const editor = page.locator('.cm-content');
		const openA = page.getByRole('link', { name: 'Open A', exact: true });
		const original = await serverText(request, a);

		await postLinks(page, request, `[Open A](<${a.docPath}>)`);
		if (label === 'desktop') {
			await openA.click();
			await expect(editor).toContainText(a.body);
		}
		try {
			// Edited A on screen, request B: a link, and A's unsaved edit stays.
			// Compact sends once from chat, then runs both gated tool calls with
			// A mounted: remounting a dirty draft would wait for this held save.
			const first = label === 'compact'
				? await queueOpen(page, request, b, 'Already open.', { then: a })
				: await queueOpen(page, request, b, 'Use [Open B](<' + b.docPath + '>)');
			if (label === 'compact') {
				await revealEarlierCompletedWork(page, openA);
				await openA.click();
			}
			await expect(editor).toContainText(a.body);
			await expectCapability(log, threadId, { canShow: false, visibleArtifactId: a.artifactId });
			await typeAtEnd(page, ' edited A');
			await expect(editor).toContainText(' edited A');
			await saves.waitForHeldSave();
			expect(await serverText(request, a)).toBe(original);
			let before = await canvasState(page, threadId);
			expect(before.editorFocused).toBe(true);
			expect(before.selectionInEditor).toBe(true);
			expectCannotShowLink(await releaseOpen(request, log, first));
			await expect.poll(() => canvasState(page, threadId)).toEqual(before);
			expect(before.tabs).toHaveLength(1);

			// Edited A on screen, request A: already open, and the edit is kept.
			const second = label === 'compact' ? thenGate(first) : await queueOpen(page, request, a, 'Already open.');
			await expect(editor).toContainText(' edited A');
			await expectCapability(log, threadId, { canShow: false, visibleArtifactId: a.artifactId });
			await typeAtEnd(page, ' still A');
			await expect(editor).toContainText(' edited A still A');
			expect(await serverText(request, a)).toBe(original);
			before = await canvasState(page, threadId);
			expect(before.editorFocused).toBe(true);
			expect(await releaseOpen(request, log, second, label === 'compact' ? 3 : 2)).toMatchObject({ outcome: 'already_open' });
			await expect.poll(() => canvasState(page, threadId)).toEqual(before);
		} finally {
			await saves.release();
		}

		// The kept draft is the one that reaches the server.
		await expect.poll(() => serverText(request, a), { timeout: 10_000 }).toContain(' edited A still A');
		if (label === 'compact') {
			await page.getByRole('button', { name: 'Chat', exact: true }).click();
			await revealWorked(page);
			await expect(page.getByText('Already open.', { exact: true })).toBeVisible();
		}
	});
}
