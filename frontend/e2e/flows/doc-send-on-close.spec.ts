import path from 'node:path';
import { expect, test, type Locator, type Page } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';

type SeededDocument = { artifactId: string; path: string; fileName: string };

async function seedDocument(request: Parameters<typeof createSession>[0], threadId: string, title: string, suffix: string): Promise<SeededDocument> {
	const rootsResponse = await request.get(`/api/files/tree?token=${TEST_TOKEN}`);
	expect(rootsResponse.ok()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await rootsResponse.json()).roots;
	const workspace = roots.find((root) => root.kind === 'workspace')?.path;
	expect(workspace).toEqual(expect.any(String));
	const fileName = `${title}-${suffix}.md`;
	const docPath = path.join(workspace!, fileName);
	const seeded = await request.put(`/api/files/content?token=${TEST_TOKEN}`, {
		data: { path: docPath, text: 'A commentable document.\n', thread_id: threadId, create: true },
	});
	expect(seeded.ok(), await seeded.text()).toBeTruthy();
	return { artifactId: (await seeded.json()).artifact.id as string, path: docPath, fileName };
}

async function addUserComment(request: Parameters<typeof createSession>[0], artifactId: string): Promise<void> {
	const created = await request.post(`/api/artifacts/${artifactId}/comments?token=${TEST_TOKEN}`, {
		data: { body: 'Deliver this as document feedback.', anchor_text: 'commentable', from_off: 2, to_off: 13 },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
}

// Narrow viewports keep tabs in the compact sheet; its close uses the same path.
async function tabControl(page: Page, fileName: string, action: 'close' | 'select'): Promise<Locator> {
	if ((page.viewportSize()?.width ?? 0) <= 768) {
		await page.getByRole('button', { name: /^Tabs \(/ }).click();
		const sheet = page.getByRole('dialog', { name: 'Open side panel items' });
		return action === 'close'
			? sheet.getByRole('button', { name: `Close ${fileName}` })
			: sheet.getByRole('button', { name: fileName, exact: true });
	}
	return action === 'close'
		? page.getByRole('button', { name: `Close ${fileName}` })
		: page.getByRole('tab', { name: fileName });
}

function workspaceState(threadId: string, documents: SeededDocument[], activeTabId: string) {
	return {
		threads: { [threadId]: {
			tabs: documents.map((document, index) => ({
				instanceId: `doc-${index}`,
				artifactId: document.artifactId,
				path: document.path,
				title: document.fileName,
				loadedRevision: null,
				latestRevision: null,
				status: 'loading',
			})),
			activeTabId,
			visible: true,
			activeSurface: 'artifact',
		} },
		splitWidth: 420,
	};
}

test('@surface:doc-send-on-close sends an open user batch exactly once before the explicit tab close', async ({ page, request }) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	const title = `send-on-close-${Date.now()}`;
	const threadId = await createSession(request, title);
	const document = await seedDocument(request, threadId, title, 'close');
	await addUserComment(request, document.artifactId);
	const sends: string[] = [];
	page.on('request', (outgoing) => {
		if (new URL(outgoing.url()).pathname === '/api/doc-feedback/send') sends.push(outgoing.url());
	});
	await page.addInitScript((state) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify(state)), workspaceState(threadId, [document], 'doc-0'));
	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.getByRole('button', { name: /Send 1 comment/ })).toBeVisible();

	await (await tabControl(page, document.fileName, 'close')).click();
	await expect.poll(() => sends).toHaveLength(1);
	await expect(page.getByRole('tab', { name: document.fileName })).toHaveCount(0);
	await expect(page.getByRole('button', { name: `Close ${document.fileName}` })).toHaveCount(0);
});

test('@surface:doc-send-on-close keeps a switch-away grace and shows its server deadline on return', async ({ page, request }) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	const title = `switch-away-with-a-long-owner-name-${Date.now()}`;
	const threadId = await createSession(request, title);
	const first = await seedDocument(request, threadId, title, 'first');
	const second = await seedDocument(request, threadId, title, 'second');
	await addUserComment(request, first.artifactId);
	const leaves: string[] = [];
	page.on('request', (outgoing) => {
		if (new URL(outgoing.url()).pathname === '/api/doc-feedback/leave') leaves.push(outgoing.url());
	});
	await page.addInitScript((state) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify(state)), workspaceState(threadId, [first, second], 'doc-0'));
	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.getByRole('button', { name: /Send 1 comment/ })).toBeVisible();

	await (await tabControl(page, second.fileName, 'select')).click();
	await expect.poll(() => leaves).toHaveLength(1);
	await (await tabControl(page, first.fileName, 'select')).click();
	await expect(page.getByRole('status').filter({ hasText: 'Sending to' })).toContainText(`Sending to ${title} in`);
	const sendNow = page.getByRole('button', { name: 'Send now' });
	await expect(sendNow).toBeVisible();

	// The long grace text gives way first: Send now keeps its whole label and the toolbar stays on one row.
	const fit = await page.evaluate(() => {
		const send = [...document.querySelectorAll<HTMLElement>('.document-header .btn')].find((el) => el.textContent?.trim() === 'Send now')!;
		const count = document.querySelector<HTMLElement>('.document-header .comment-status')!;
		const review = document.querySelector<HTMLElement>('.document-header .review-request')!;
		// Measure the label itself: at phone width the button's ::after hit area adds 8 px to its scrollWidth.
		const label = document.createRange();
		label.selectNodeContents(send);
		const style = getComputedStyle(send);
		const clipped = label.getBoundingClientRect().right - (send.getBoundingClientRect().right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth));
		// Only the owner name may truncate: the countdown at the end of the grace text must be whole.
		const grace = document.querySelector<HTMLElement>('.document-header .sending-grace')!;
		const tail = document.createRange();
		tail.setStart(grace.lastChild!, 0);
		tail.setEnd(grace.lastChild!, grace.lastChild!.textContent!.length);
		const countdownClipped = tail.getBoundingClientRect().right - grace.getBoundingClientRect().right;
		const owner = grace.querySelector<HTMLElement>('.grace-owner');
		return { clipped, countdownClipped, ownerTruncated: !!owner && owner.scrollWidth > owner.clientWidth, sendTop: send.getBoundingClientRect().top, countTop: count.getBoundingClientRect().top, reviewTop: review.getBoundingClientRect().top };
	});
	expect(fit.clipped, 'Send now label is not truncated').toBeLessThanOrEqual(0.5);
	expect(fit.countdownClipped, 'the "in N min" countdown is not clipped').toBeLessThanOrEqual(0.5);
	expect(fit.ownerTruncated, 'the long owner name is what truncates').toBe(true);
	expect(Math.abs(fit.reviewTop - fit.countTop), 'Request review shares a row with the comment count').toBeLessThanOrEqual(4);
	expect(Math.abs(fit.sendTop - fit.countTop), 'Send now shares that row').toBeLessThanOrEqual(4);
});

// The pinned header once widened the canvas at phone width: its "Send N comments to <owner>" button pushed the editor sideways.
test('@surface:doc-send-on-close at 393 px the pinned header with a pending send fits inside the canvas', async ({ page, request }) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	const title = `header-fits-a-long-owner-thread-title-${Date.now()}`;
	const threadId = await createSession(request, title);
	const narrow = await seedDocument(request, threadId, title, 'narrow');
	await addUserComment(request, narrow.artifactId);
	await addUserComment(request, narrow.artifactId);
	await page.setViewportSize({ width: 393, height: 852 });
	await page.addInitScript((state) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify(state)), workspaceState(threadId, [narrow], 'doc-0'));
	await loginByUrl(page);
	await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.getByRole('button', { name: /^Send 2 comments/ })).toBeVisible();
	await expect(page.locator('.cm-content')).toBeVisible();

	const layout = await page.evaluate(() => {
		const doc = document.querySelector<HTMLElement>('.doc')!;
		const scroller = document.querySelector<HTMLElement>('.cm-scroller')!;
		const bounds = doc.getBoundingClientRect();
		const outside = [...document.querySelectorAll<HTMLElement>('.document-header button')]
			.filter((button) => button.getClientRects().length > 0)
			.map((button) => ({ name: button.getAttribute('aria-label') ?? button.textContent?.trim() ?? '', rect: button.getBoundingClientRect() }))
			.filter(({ rect }) => rect.left < bounds.left - 0.5 || rect.right > bounds.right + 0.5)
			.map(({ name, rect }) => `${name}: ${Math.round(rect.left)}..${Math.round(rect.right)} outside ${Math.round(bounds.left)}..${Math.round(bounds.right)}`);
		return {
			overflow: { doc: doc.scrollWidth - doc.clientWidth, scroller: scroller.scrollWidth - scroller.clientWidth, page: document.documentElement.scrollWidth - window.innerWidth },
			outside,
		};
	});
	expect(layout.overflow, 'the canvas, the editor and the page have nothing to scroll sideways').toEqual({ doc: 0, scroller: 0, page: 0 });
	expect(layout.outside, 'every header button lies inside the canvas').toEqual([]);
});
