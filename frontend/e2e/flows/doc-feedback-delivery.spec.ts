import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from '../support/test';
import { expect } from '@playwright/test';
import { showAllThreads } from '../support/commentDrawer';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { deliveredBatches, headerSend, heldIn, leaveUnsent, setDeliveryPreference } from '../support/heldComments';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

async function captureDeliveryFrame(page: import('@playwright/test').Page, name: string): Promise<void> {
	const directory = process.env.CORSIA_FRAME_DIR;
	if (!directory) return;
	const viewport = page.viewportSize();
	await page.screenshot({ path: path.join(directory, `${name}-${viewport?.width ?? 0}x${viewport?.height ?? 0}.png`), fullPage: true });
}

async function expectHeaderSendFits(send: import('@playwright/test').Locator): Promise<void> {
	const probe = () => send.evaluate((button) => {
		const box = button.getBoundingClientRect();
		const style = getComputedStyle(button);
		const slot = button.closest<HTMLElement>('.sending-now');
		const identity = button.closest<HTMLElement>('.document-identity');
		return {
			fits: button.scrollWidth <= button.clientWidth && box.left >= 0 && box.right <= button.ownerDocument.defaultView!.innerWidth,
			button: { clientWidth: button.clientWidth, scrollWidth: button.scrollWidth, width: box.width, minWidth: style.minWidth, maxWidth: style.maxWidth, flex: style.flex },
			slot: slot && { width: slot.getBoundingClientRect().width, minWidth: getComputedStyle(slot).minWidth, maxWidth: getComputedStyle(slot).maxWidth, flex: getComputedStyle(slot).flex },
			identity: identity && { width: identity.getBoundingClientRect().width, display: getComputedStyle(identity).display, flexWrap: getComputedStyle(identity).flexWrap },
		};
	});
	// Wait for the control's entrance motion to settle, then prove the full label fits rather than only being visible.
	await expect.poll(async () => (await probe()).fits).toBe(true);
}

function seedAgentComment(artifactId: string, commentId: string, threadId: string): void {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !stateRoot) {
		throw new Error('Agent-comment fixture requires isolated E2E mock state');
	}
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', [
			'import sqlite3, sys',
			'connection = sqlite3.connect(sys.argv[1])',
			'connection.execute("DELETE FROM doc_feedback_batches WHERE author_kind=\'user\' AND artifact_id=?", (sys.argv[2],))',
			'changed = connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Reviewer", sys.argv[4], sys.argv[3])).rowcount',
			'assert changed == 1, changed',
			'connection.commit()',
			'connection.close()',
		].join('\n'), path.join(stateRoot, 'corsia.db'), artifactId, commentId, threadId,
	], { cwd: repoRoot, encoding: 'utf8' });
}

function deliveredOwnerTask(artifactId: string): { title: string; description: string } | null {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !stateRoot) {
		throw new Error('Delivered-task fixture requires isolated E2E mock state');
	}
	const output = execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', [
			'import json, sqlite3, sys',
			'connection = sqlite3.connect(sys.argv[1])',
			'row = connection.execute("SELECT n.title,n.description FROM doc_feedback_batches b JOIN plan_nodes n ON n.id=b.delivered_node_id WHERE b.artifact_id=? AND b.state=\'delivered\' ORDER BY b.updated_at DESC LIMIT 1", (sys.argv[2],)).fetchone()',
			'print(json.dumps(dict(zip(("title", "description"), row)) if row else None))',
			'connection.close()',
		].join('\n'), path.join(stateRoot, 'corsia.db'), artifactId,
	], { cwd: repoRoot, encoding: 'utf8' });
	return JSON.parse(output) as { title: string; description: string } | null;
}

test('@surface:doc-feedback-delivery sends a held user batch from the header in the open canvas', async ({ page, request }, testInfo) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	if (testInfo.project.name === 'chromium-mobile') await page.setViewportSize({ width: 393, height: 852 });
	let documentReloads = 0;
	let navigations = 0;
	page.on('request', (outgoing) => {
		if (new URL(outgoing.url()).pathname === '/api/files/content') documentReloads += 1;
	});
	page.on('framenavigated', (frame) => {
		if (frame === page.mainFrame()) navigations += 1;
	});
	const stamp = Date.now();
	const title = `feedback-delivery-${stamp}`;
	const threadId = await createSession(request, title);
	const token = encodeURIComponent(TEST_TOKEN);
	// The marker sits by the lower viewport edge: body text may sit under the transient notice, but this control may not.
	const longDocument = [...Array.from({ length: 28 }, (_, index) => `Paragraph ${index + 1} fills the canvas behind the transient delivery notice.`), 'A comment that will become due.'].join('\n\n');
	const commentOffset = longDocument.lastIndexOf('comment');
	const rootsResponse = await request.get(`/api/files/tree?token=${token}`);
	expect(rootsResponse.ok()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await rootsResponse.json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}.md`;
	const docPath = path.join(workspace!, fileName);
	const seeded = await request.put(`/api/files/content?token=${token}`, {
		data: { path: docPath, text: longDocument, thread_id: threadId, create: true },
	});
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId = (await seeded.json()).artifact.id as string;
	const created = await request.post(`/api/artifacts/${artifactId}/comments?token=${token}`, {
		data: { body: 'This pending comment is delivered by the due-batch seam.', anchor_text: 'comment', from_off: commentOffset, to_off: commentOffset + 'comment'.length, delivery_policy: 'immediate' },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	await leaveUnsent(request, (await created.json()).comment.id);

	await page.addInitScript(({ threadId, docPath, fileName }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: {
				tabs: [{ instanceId: 'feedback-tab', artifactId: null, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }],
				activeTabId: 'feedback-tab', visible: true, activeSurface: 'artifact',
			} }, splitWidth: 420,
		}));
	}, { threadId, docPath, fileName });
	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await page.locator('.cm-scroller').evaluate((scroller) => { scroller.scrollTop = scroller.scrollHeight; });
	await expect(page.locator('.cm-comment-marker')).toBeVisible();
	// The held batch waits for the header's Send, which goes once the batch is delivered.
	await expect(headerSend(page)).toHaveText('Send 1 comment');
	await expectHeaderSendFits(headerSend(page));
	await captureDeliveryFrame(page, 'header-send');
	await page.evaluate(() => {
		(window as Window & { feedbackVisibilityChanges?: number }).feedbackVisibilityChanges = 0;
		document.addEventListener('visibilitychange', () => {
			const current = window as Window & { feedbackVisibilityChanges?: number };
			current.feedbackVisibilityChanges = (current.feedbackVisibilityChanges ?? 0) + 1;
		});
	});
	documentReloads = 0;
	navigations = 0;
	await headerSend(page).click();
	const confirmation = page.locator('.document-canvas .delivery-confirmation .toast--success');
	await expect(confirmation).toContainText('1 comment sent');
	await captureDeliveryFrame(page, 'canvas-confirmation');
	// Body text may sit behind this transient notice, but no control, marker, pin, composer, or scrollbar may.
	const [confirmationBox, canvasBox] = await Promise.all([
		confirmation.boundingBox(),
		page.locator('.document-canvas').boundingBox(),
	]);
	expect(confirmationBox).not.toBeNull();
	expect(canvasBox).not.toBeNull();
	expect(confirmationBox!.y).toBeGreaterThanOrEqual(canvasBox!.y);
	expect(confirmationBox!.y + confirmationBox!.height).toBeLessThanOrEqual(canvasBox!.y + canvasBox!.height);
	const overlap = await page.locator('.document-canvas').evaluate((canvas) => {
		const notice = canvas.querySelector<HTMLElement>('.delivery-confirmation .toast');
		if (!notice) throw new Error('Expected canvas-local confirmation');
		const noticeBox = notice.getBoundingClientRect();
		const intersects = (box: DOMRect) => noticeBox.left < box.right && noticeBox.right > box.left
			&& noticeBox.top < box.bottom && noticeBox.bottom > box.top;
		const blocked = Array.from(canvas.querySelectorAll<HTMLElement>('button, [role="button"], .cm-comment-marker, [data-region-thumb]'))
			.filter((element) => {
				const style = getComputedStyle(element);
				return !notice.contains(element) && style.display !== 'none' && style.visibility !== 'hidden' && intersects(element.getBoundingClientRect());
			})
			.map((element) => element.className || element.getAttribute('aria-label') || element.tagName);
		const scroller = canvas.querySelector<HTMLElement>('.cm-scroller');
		const gutter = scroller ? scroller.offsetWidth - scroller.clientWidth : 0;
		const scrollerBox = scroller?.getBoundingClientRect();
		const scrollbar = gutter > 0 && !!scrollerBox && intersects(new DOMRect(scrollerBox.right - gutter, scrollerBox.top, gutter, scrollerBox.height));
		return { blocked, markerCount: canvas.querySelectorAll('.cm-comment-marker').length, scrollbar };
	});
	expect(overlap.markerCount, 'the lower canvas edge includes the comment marker worst case').toBeGreaterThan(0);
	expect(overlap.blocked, 'canvas confirmation never covers an interactive control or marker').toEqual([]);
	expect(overlap.scrollbar, 'canvas confirmation never covers the editor scrollbar').toBe(false);
	expect(await page.evaluate(() => document.querySelector('.toast-container')?.textContent ?? '')).not.toContain('1 comment sent');
	await expect(headerSend(page)).toHaveCount(0);
	await expect.poll(() => heldIn(artifactId, 'document-held')).toBe(0);
	expect(heldIn(artifactId, 'document-held')).toBe(0);
	expect(deliveredBatches(artifactId, 'document-held')).toBe(1);
	expect(documentReloads).toBe(0);
	expect(navigations).toBe(0);
	expect(await page.evaluate(() =>
		(window as Window & { feedbackVisibilityChanges?: number }).feedbackVisibilityChanges,
	)).toBe(0);
});

// A re-open follows the person's delivery preference like a new comment: held for the header's Send when it is off, sent at once when it is on.
for (const deliverImmediately of [false, true]) test(`@surface:doc-reopen-feedback-delivery sends a re-opened comment to the document owner (deliver immediately ${deliverImmediately ? 'on' : 'off'})`, async ({ page, request }) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	const title = `reopen-feedback-${Date.now()}`;
	const threadId = await createSession(request, title);
	const token = TEST_TOKEN;
	const rootsResponse = await request.get(`/api/files/tree?token=${token}`);
	expect(rootsResponse.ok()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await rootsResponse.json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}.md`;
	const docPath = path.join(workspace!, fileName);
	const seeded = await request.put(`/api/files/content?token=${token}`, {
		data: { path: docPath, text: 'An agent comment can be resolved and re-opened.\n', thread_id: threadId, create: true },
	});
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	const artifactId = (await seeded.json()).artifact.id as string;
	const created = await request.post(`/api/artifacts/${artifactId}/comments?token=${token}`, {
		data: { body: 'Please revisit this comment.', anchor_text: 'comment', from_off: 9, to_off: 16, delivery_policy: 'immediate' },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	const commentId = (await created.json()).comment.id as string;
	seedAgentComment(artifactId, commentId, threadId);

	await setDeliveryPreference(request, deliverImmediately);
	await page.addInitScript(({ threadId, docPath, fileName }) => {
		localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
			threads: { [threadId]: {
				tabs: [{ instanceId: 'reopen-tab', artifactId: null, path: docPath, title: fileName, loadedRevision: null, latestRevision: null, status: 'loading' }],
				activeTabId: 'reopen-tab', visible: true, activeSurface: 'artifact',
			} }, splitWidth: 420,
		}));
	}, { threadId, docPath, fileName });
	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await page.getByRole('button', { name: /^1 open thread\b/ }).click();
	await showAllThreads(page.getByRole('region', { name: 'Document comments', exact: true }));
	const comment = page.getByRole('region', { name: /^Comment by / });
	await expect(comment).toContainText('Please revisit this comment.');
	await comment.getByRole('button', { name: 'Resolve', exact: true }).click();
	const resolved = page.getByRole('region', { name: /^Resolved comment by / });
	await expect(resolved).toBeVisible();
	await resolved.getByRole('button', { name: 'Resolved · 1 message' }).click();
	// The document has an owner, so the card says a re-open reaches it.
	await expect(resolved).toContainText('Re-open asks the agent to revisit.');
	await resolved.getByRole('button', { name: 'Re-open', exact: true }).click();
	// The re-opened comment is held for the owner: the header's Send is its one sender, reachable once the drawer is closed.
	await page.getByRole('region', { name: 'Document comments', exact: true }).getByRole('button', { name: 'Close comments', exact: true }).click();
	if (deliverImmediately) await expect(headerSend(page)).toHaveCount(0);
	else {
		await expect(headerSend(page)).toHaveText('Send 1 comment');
		await headerSend(page).click();
	}
	await expect.poll(() => deliveredOwnerTask(artifactId), { timeout: 20_000 }).toMatchObject({
		title: expect.stringMatching(/re-opened 1 comment/),
		description: expect.stringContaining(`Comment ${commentId} (re-opened by`),
	});
});
