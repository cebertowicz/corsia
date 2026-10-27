import { test } from '../support/test';
import { expect } from '@playwright/test';
import { chatComposer } from '../support/corsia';
import {
	activeArtifactId,
	canvasState,
	expectCannotShowLink,
	expectCapability,
	openThreadView,
	postLinks,
	queueOpen,
	releaseOpen,
	revealWorked,
	watchSocket,
} from '../support/documentOpen';

// Each layout runs in its matching browser mode: 1440 in chromium-desktop and
// 393 in chromium-mobile. Every queued request is released only after the
// server has acknowledged the claim and the page has published the canvas
// fact the request must be judged against.

test(`@surface:agent-opens-document desktop reveals into an empty or hidden leaf and links past a visible document`, async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Desktop layout runs in the desktop browser mode.');
	test.setTimeout(90_000);
	await page.setViewportSize({ width: 1440, height: 1000 });
	const log = watchSocket(page);
	const { threadId, a, b } = await openThreadView(page, request, log, 'desktop');
	const editor = page.locator('.cm-content');

	// Empty leaf: the request reveals A beside the chat the user is in.
	await expectCapability(log, threadId, { canShow: true, visibleArtifactId: null });
	let gate = await queueOpen(page, request, a, 'Requested A.');
	expect(await releaseOpen(request, log, gate)).toMatchObject({ outcome: 'requested', count: 1 });
	await expect.poll(() => activeArtifactId(page, threadId)).toBe(a.artifactId);
	await expect.poll(async () => (await canvasState(page, threadId)).tabs).toHaveLength(1);
	await expect.poll(async () => (await canvasState(page, threadId)).visible).toBe(true);
	await expect.poll(async () => (await canvasState(page, threadId)).activeSurface).toBe('chat');
	await expect(editor).toContainText(a.body);

	// Visible A, request A: already open; selection, focus, and canvas stay.
	await expectCapability(log, threadId, { canShow: false, visibleArtifactId: a.artifactId });
	gate = await queueOpen(page, request, a, 'Already open.');
	let before = await canvasState(page, threadId);
	expect(await releaseOpen(request, log, gate)).toMatchObject({ outcome: 'already_open' });
	await expect.poll(() => canvasState(page, threadId)).toEqual(before);

	// Visible A, request B: a link, and A stays on screen.
	gate = await queueOpen(page, request, b, 'Use [Open B](<' + b.docPath + '>)');
	before = await canvasState(page, threadId);
	expectCannotShowLink(await releaseOpen(request, log, gate));
	await expect.poll(() => canvasState(page, threadId)).toEqual(before);
	await revealWorked(page);
	await expect(page.getByRole('link', { name: 'Open B', exact: true })).toBeVisible();

	// Hidden leaf: nothing is on screen, so a request for A reveals its tab.
	await page.getByRole('button', { name: 'Collapse Files' }).click();
	await expect.poll(async () => (await canvasState(page, threadId)).visible).toBe(false);
	await expectCapability(log, threadId, { canShow: true, visibleArtifactId: null });
	gate = await queueOpen(page, request, a, 'Requested A again.');
	const hiddenTabs = (await canvasState(page, threadId)).tabs;
	expect(await releaseOpen(request, log, gate)).toMatchObject({ outcome: 'requested', count: 1 });
	await expect.poll(async () => (await canvasState(page, threadId)).visible).toBe(true);
	await expect.poll(async () => (await canvasState(page, threadId)).tabs).toEqual(hiddenTabs);
	await expect.poll(() => activeArtifactId(page, threadId)).toBe(a.artifactId);
	await expect(editor).toContainText(a.body);

	// The link is the user's route to B: tapping it opens exactly B.
	await revealWorked(page);
	await page.getByRole('link', { name: 'Open B', exact: true }).last().click();
	await expect.poll(() => activeArtifactId(page, threadId)).toBe(b.artifactId);
	await expect.poll(async () => (await canvasState(page, threadId)).tabs).toHaveLength(2);
	await expect(editor).toContainText(b.name);
	await expect(editor).toContainText(b.body);
	await expect(editor).not.toContainText(a.body);
});

test(`@surface:agent-opens-document desktop links a background tab while another is visible and reveals it from a hidden leaf`, async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'Desktop layout runs in the desktop browser mode.');
	test.setTimeout(90_000);
	await page.setViewportSize({ width: 1440, height: 1000 });
	const log = watchSocket(page);
	const { threadId, a, b } = await openThreadView(page, request, log, 'desktop-background');
	const editor = page.locator('.cm-content');

	await postLinks(page, request, `[Open A](<${a.docPath}>) and [Open B](<${b.docPath}>)`);
	await page.getByRole('link', { name: 'Open A', exact: true }).click();
	await expect(editor).toContainText(a.body);
	await page.getByRole('link', { name: 'Open B', exact: true }).click();
	await expect(editor).toContainText(b.body);
	await page.getByRole('tab', { name: a.name }).click();
	await expect(editor).toContainText(a.body);
	await expect.poll(() => activeArtifactId(page, threadId)).toBe(a.artifactId);

	// B is open but in the background behind visible A: a link, not a switch.
	await expectCapability(log, threadId, { canShow: false, visibleArtifactId: a.artifactId });
	let gate = await queueOpen(page, request, b, 'Use [Open B](<' + b.docPath + '>)');
	const before = await canvasState(page, threadId);
	expectCannotShowLink(await releaseOpen(request, log, gate));
	await expect.poll(() => canvasState(page, threadId)).toEqual(before);
	await expect(editor).toContainText(a.body);

	// With the leaf hidden, the same request selects and reveals the existing B.
	await page.getByRole('button', { name: 'Collapse Files' }).click();
	await expectCapability(log, threadId, { canShow: true, visibleArtifactId: null });
	gate = await queueOpen(page, request, b, 'Requested B.');
	expect(await releaseOpen(request, log, gate)).toMatchObject({ outcome: 'requested', count: 1 });
	await expect.poll(async () => (await canvasState(page, threadId)).visible).toBe(true);
	await expect.poll(() => activeArtifactId(page, threadId)).toBe(b.artifactId);
	await expect.poll(async () => (await canvasState(page, threadId)).tabs).toEqual(before.tabs);
	await expect(editor).toContainText(b.body);
});

test(`@surface:agent-opens-document compact links from chat and never replaces the visible document`, async ({ page, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-mobile', 'Compact layout runs in the mobile browser mode.');
	test.setTimeout(120_000);
	await page.setViewportSize({ width: 393, height: 1000 });
	const log = watchSocket(page);
	const { threadId, a, b } = await openThreadView(page, request, log, 'compact');
	const editor = page.locator('.cm-content');
	const chatButton = page.getByRole('button', { name: 'Chat', exact: true });
	const openA = page.getByRole('link', { name: 'Open A', exact: true });

	// Compact shows one surface, so an agent can never put a document on it:
	// even an empty canvas gets a link the user taps.
	await expectCapability(log, threadId, { canShow: false, visibleArtifactId: null });
	let gate = await queueOpen(page, request, a, 'Use [Open A](<' + a.docPath + '>)');
	expectCannotShowLink(await releaseOpen(request, log, gate));
	expect((await canvasState(page, threadId)).tabs ?? []).toEqual([]);
	await expect(chatComposer(page)).toBeVisible();
	await revealWorked(page);
	await page.getByRole('link', { name: 'Open A', exact: true }).click();
	await expect(editor).toContainText(a.body);
	await expect.poll(() => activeArtifactId(page, threadId)).toBe(a.artifactId);
	await expectCapability(log, threadId, { canShow: false, visibleArtifactId: a.artifactId });

	// Chat on screen with A in the background: still a link.
	await chatButton.click();
	await expectCapability(log, threadId, { canShow: false, visibleArtifactId: null });
	gate = await queueOpen(page, request, b, 'Use [Open B](<' + b.docPath + '>)');
	let before = await canvasState(page, threadId);
	expectCannotShowLink(await releaseOpen(request, log, gate));
	await expect.poll(() => canvasState(page, threadId)).toEqual(before);
	expect(before.activeSurface).toBe('chat');
	await revealWorked(page);

	// Visible A, request A. The reply is hidden while A owns the canvas, so the
	// canvas is compared before returning to chat.
	gate = await queueOpen(page, request, a, 'Already open.');
	// Leaving the composer changes compact keyboard spacing. Settle focus
	// before the pointer press so the link stays under its release.
	await openA.focus();
	await openA.click();
	await expect(editor).toContainText(a.body);
	await expectCapability(log, threadId, { canShow: false, visibleArtifactId: a.artifactId });
	before = await canvasState(page, threadId);
	expect(await releaseOpen(request, log, gate)).toMatchObject({ outcome: 'already_open' });
	await expect.poll(() => canvasState(page, threadId)).toEqual(before);
	await chatButton.click();
	await revealWorked(page);
	await expect(page.getByText('Already open.', { exact: true })).toBeVisible();

	// Visible A, request B: a link; A keeps the canvas.
	gate = await queueOpen(page, request, b, 'Use [Open B](<' + b.docPath + '>)');
	await openA.focus();
	await openA.click();
	await expect(editor).toContainText(a.body);
	await expectCapability(log, threadId, { canShow: false, visibleArtifactId: a.artifactId });
	before = await canvasState(page, threadId);
	expectCannotShowLink(await releaseOpen(request, log, gate));
	await expect.poll(() => canvasState(page, threadId)).toEqual(before);

	// Tapping the link opens exactly B.
	await chatButton.click();
	await revealWorked(page);
	// The newest link sits under the composer, so the press scrolls it into view first, and leaving the live edge
	// drops the list's bottom spacing, which slides the link out from under the pointer mid-press. Scroll it
	// to the middle and wait for the list to stop moving, so the press lands on a link that is not about to move.
	const openB = page.getByRole('link', { name: 'Open B', exact: true }).last();
	await openB.evaluate((link) => link.scrollIntoView({ block: 'center' }));
	let previous = '';
	await expect.poll(async () => {
		const box = JSON.stringify(await openB.boundingBox());
		const still = box === previous;
		previous = box;
		return still;
	}, { intervals: [100] }).toBe(true);
	await openB.click();
	await expect.poll(() => activeArtifactId(page, threadId)).toBe(b.artifactId);
	await expect.poll(async () => (await canvasState(page, threadId)).tabs).toHaveLength(2);
	await expect(editor).toContainText(b.name);
	await expect(editor).toContainText(b.body);
});
