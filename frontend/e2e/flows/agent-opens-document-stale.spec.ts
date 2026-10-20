import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { releaseMockGate } from '../support/mock';
import {
	activeArtifactId,
	canvasState,
	expectCapability,
	newSocketLog,
	openThreadView,
	postLinks,
	queueOpen,
	releaseOpen,
	type TestDocument,
} from '../support/documentOpen';
import { routeSocketHoldingOpenRequest } from '../support/documentOpenHolds';

// A request is judged against the fact the page last published, but the view
// can change before the frame arrives. The real backend frame is held at the
// socket while the user changes the view, then delivered unchanged: the
// page's own receipt guard must decline it.
//
// Both receipts start from an eligible desktop view, the only state in which
// the server sends a request, so both run in the desktop browser mode; the
// compact receipt is reached by resizing that same page to 393.

function finishedTurns(page: Page) {
	// Empty groups have the bare label "Worked"; only a completed turn has
	// the step/duration summary used as this frame-processing barrier.
	return page.getByRole('button', { name: /^Expand Worked ·/ });
}

for (const receipt of ['desktop', 'compact'] as const) {
	test(`@surface:agent-opens-document ${receipt} receipt declines a request that arrives after the view changed`, async ({ page, request }, testInfo) => {
		test.skip(testInfo.project.name !== 'chromium-desktop', 'The server only sends a request to an eligible desktop view.');
		test.setTimeout(90_000);
		await page.setViewportSize({ width: 1440, height: 1000 });
		const log = newSocketLog();
		let target: TestDocument | null = null;
		const socket = await routeSocketHoldingOpenRequest(page, log, (frame) => (
			frame.artifact_id === target?.artifactId
		));
		const { threadId, a, b } = await openThreadView(page, request, log, `stale-${receipt}`);
		target = b;
		if (receipt === 'desktop') await postLinks(page, request, `[Open A](<${a.docPath}>)`);
		await expect(finishedTurns(page)).toHaveCount(0);

		// Eligible empty canvas: the server sends B's request, which is held.
		await expectCapability(log, threadId, { canShow: true, visibleArtifactId: null });
		const gate = await queueOpen(page, request, b, 'Requested B.', { holdCompletion: true });
		expect(await releaseOpen(request, log, gate)).toMatchObject({ outcome: 'requested', count: 1 });
		await socket.waitForHeld();

		// The user changes the view before the frame arrives.
		let unsafe: { canShow: boolean; visibleArtifactId: string | null };
		if (receipt === 'desktop') {
			await page.getByRole('link', { name: 'Open A', exact: true }).click();
			await expect(page.locator('.cm-content')).toContainText(a.body);
			unsafe = { canShow: false, visibleArtifactId: a.artifactId };
		} else {
			await page.setViewportSize({ width: 393, height: 1000 });
			unsafe = { canShow: false, visibleArtifactId: null };
		}
		await expectCapability(log, threadId, unsafe);
		const before = await canvasState(page, threadId);

		// Deliver the held frame, then let the reply through behind it on the
		// same socket; the finished turn proves the page handled the frame.
		socket.releaseHeld();
		await releaseMockGate(request, `${gate}-reply`);
		await expect(finishedTurns(page)).toHaveCount(1, { timeout: 20_000 });

		expect(await canvasState(page, threadId)).toEqual(before);
		expect(log.capabilities.get(threadId)).toEqual(unsafe);
		if (receipt === 'desktop') {
			expect(before.tabs).toHaveLength(1);
			expect(await activeArtifactId(page, threadId)).toBe(a.artifactId);
		} else {
			expect(before.tabs ?? []).toEqual([]);
		}
		expect(before.tabs?.some((tab: { artifactId: string | null }) => tab.artifactId === b.artifactId) ?? false).toBe(false);
	});
}
