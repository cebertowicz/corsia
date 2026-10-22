import path from 'node:path';
import { test } from '../support/test';
import { expect, type Locator, type Page } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { leaveUnsent, openUnsentFooter, visibleHeaderSends } from '../support/docFeedbackFooter';

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

async function addUserComment(request: Parameters<typeof createSession>[0], artifactId: string): Promise<string> {
	const created = await request.post(`/api/artifacts/${artifactId}/comments?token=${TEST_TOKEN}`, {
		data: { body: 'Deliver this as document feedback.', anchor_text: 'commentable', from_off: 2, to_off: 13 },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	return (await created.json()).comment.id as string;
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

test('@surface:doc-send-on-close sends an unsent user batch exactly once before the explicit tab close', async ({ page, request }) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	const title = `send-on-close-${Date.now()}`;
	const threadId = await createSession(request, title);
	const document = await seedDocument(request, threadId, title, 'close');
	await leaveUnsent(request, await addUserComment(request, document.artifactId));
	const sends: string[] = [];
	page.on('request', (outgoing) => {
		if (new URL(outgoing.url()).pathname === '/api/doc-feedback/send') sends.push(outgoing.url());
	});
	await page.addInitScript((state) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify(state)), workspaceState(threadId, [document], 'doc-0'));
	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	// Send now is in the comments drawer's footer, which says when the batch goes; the header has none.
	const footer = await openUnsentFooter(page);
	await expect(footer).toContainText(`1 message not sent yet · it goes to ${title} once you stop typing.`);
	await expect(footer.getByRole('button', { name: 'Send now', exact: true })).toBeVisible();
	expect(await visibleHeaderSends(page), 'no Send shows in the header').toEqual([]);

	// A phone's comments sheet is modal over the document column, so it closes before the tab list is reachable (RSR-7).
	if ((page.viewportSize()?.width ?? 0) <= 768) {
		await page.getByRole('button', { name: 'Close comments', exact: true }).click();
		await expect(page.locator('[data-doc-comments-drawer]')).toHaveCount(0);
		expect(sends, 'closing the sheet alone sends nothing').toHaveLength(0);
	}
	// Closing the tab sends what is still unsent, once, without Send now.
	await (await tabControl(page, document.fileName, 'close')).click();
	await expect.poll(() => sends).toHaveLength(1);
	await expect(page.getByRole('tab', { name: document.fileName })).toHaveCount(0);
	await expect(page.getByRole('button', { name: `Close ${document.fileName}` })).toHaveCount(0);
});

test('@surface:doc-send-on-close keeps its unsent footer whole beside a long owner name', async ({ page, request }) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	const title = `switch-away-with-a-long-owner-name-${Date.now()}`;
	const threadId = await createSession(request, title);
	const first = await seedDocument(request, threadId, title, 'first');
	await leaveUnsent(request, await addUserComment(request, first.artifactId));
	const sends: string[] = [];
	page.on('request', (outgoing) => {
		const pathname = new URL(outgoing.url()).pathname;
		if (pathname === '/api/doc-feedback/send') sends.push(outgoing.url());
	});
	await page.addInitScript((state) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify(state)), workspaceState(threadId, [first], 'doc-0'));
	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.getByRole('button', { name: /^1 open thread, 1 message not sent yet$/ })).toBeVisible();

	const footer = await openUnsentFooter(page);
	await expect(footer.getByRole('status')).toContainText(`1 message not sent yet · it goes to ${title} once you stop typing.`);
	const sendNow = footer.getByRole('button', { name: 'Send now', exact: true });
	await expect(sendNow).toBeVisible();
	expect(await visibleHeaderSends(page), 'neither Send nor its countdown shows in the header').toEqual([]);
	const tops = await page.evaluate(() => ({
		count: document.querySelector<HTMLElement>('.document-header .comment-status')!.getBoundingClientRect().top,
		review: document.querySelector<HTMLElement>('.document-header .review-request')!.getBoundingClientRect().top,
	}));
	expect(Math.abs(tops.review - tops.count), 'Request review shares a row with the comment count').toBeLessThanOrEqual(4);

	// The footer is pinned at the drawer's foot and opaque; its whole text (the long owner name) wraps, never clips.
	const fit = await footer.evaluate((element) => {
		const drawer = element.closest<HTMLElement>('[data-doc-comments-drawer]')!;
		const list = drawer.querySelector<HTMLElement>('.drawer-comments')!;
		const text = element.querySelector<HTMLElement>('[role="status"]')!;
		const send = element.querySelector<HTMLElement>('.btn')!;
		const label = document.createRange();
		label.selectNodeContents(send);
		const style = getComputedStyle(send);
		const probe = document.createElement('div');
		probe.style.background = 'var(--bg-secondary)';
		drawer.append(probe);
		const token = getComputedStyle(probe).backgroundColor;
		probe.remove();
		const box = element.getBoundingClientRect(), frame = drawer.getBoundingClientRect();
		return {
			labelClipped: label.getBoundingClientRect().right - (send.getBoundingClientRect().right - parseFloat(style.paddingRight) - parseFloat(style.borderRightWidth)),
			textClipped: Math.max(text.scrollWidth - text.clientWidth, text.scrollHeight - text.clientHeight),
			bottomGap: frame.bottom - box.bottom, insideX: box.left >= frame.left - 0.5 && box.right <= frame.right + 0.5,
			belowList: box.top >= list.getBoundingClientRect().bottom - 0.5,
			background: getComputedStyle(element).backgroundColor, token,
		};
	});
	expect(fit.labelClipped, 'Send now label is not truncated').toBeLessThanOrEqual(0.5);
	expect(fit.textClipped, 'the owner name and the line are whole').toBeLessThanOrEqual(0);
	expect(fit.bottomGap, 'the footer sits on the drawer\'s bottom border').toBeLessThanOrEqual(1.5);
	expect(fit.insideX, 'the footer stays inside the drawer').toBe(true);
	expect(fit.belowList, 'the comment list scrolls above the footer, not under it').toBe(true);
	expect(fit.background, 'the footer is opaque --bg-secondary').toBe(fit.token);

	// Send now uses the batch's own Send: one send, then the footer goes.
	await sendNow.click();
	await expect.poll(() => sends).toHaveLength(1);
	await expect(footer).toHaveCount(0);
});

// The pinned header once widened the canvas at phone width: its "Send N comments to <owner>" button pushed the editor sideways.
test('@surface:doc-send-on-close at 393 px the pinned header with a pending send fits inside the canvas', async ({ page, request }) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	const title = `header-fits-a-long-owner-thread-title-${Date.now()}`;
	const threadId = await createSession(request, title);
	const narrow = await seedDocument(request, threadId, title, 'narrow');
	const waiting = [await addUserComment(request, narrow.artifactId), await addUserComment(request, narrow.artifactId)];
	for (const id of waiting) await leaveUnsent(request, id);
	await page.setViewportSize({ width: 393, height: 852 });
	await page.addInitScript((state) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify(state)), workspaceState(threadId, [narrow], 'doc-0'));
	await loginByUrl(page);
	await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByText(title, { exact: true }).first().click();
	await expect(page.getByRole('button', { name: /^2 open threads, 2 messages not sent yet$/ })).toBeVisible();
	await expect(page.locator('.cm-content')).toBeVisible();
	expect(await visibleHeaderSends(page), 'no Send shows in the header').toEqual([]);

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
			overflow: { doc: doc.scrollWidth - doc.clientWidth, scroller: scroller.scrollWidth - scroller.clientWidth, page: Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth) },
			outside,
		};
	});
	expect(layout.overflow, 'the canvas, the editor and the page have nothing to scroll sideways').toEqual({ doc: 0, scroller: 0, page: 0 });
	expect(layout.outside, 'every header button lies inside the canvas').toEqual([]);

	// The phone drawer ends in the footer: inside the canvas, a 44 px Send now under a coarse pointer.
	const footer = await openUnsentFooter(page);
	await expect(footer).toContainText(`2 messages not sent yet · they go to ${title} once you stop typing.`);
	const send = footer.getByRole('button', { name: 'Send now', exact: true });
	const footerBox = (await footer.boundingBox())!, sendBox = (await send.boundingBox())!;
	expect(footerBox.x).toBeGreaterThanOrEqual(0);
	expect(footerBox.x + footerBox.width).toBeLessThanOrEqual(393);
	if (await page.evaluate(() => matchMedia('(pointer: coarse)').matches)) expect(sendBox.height, 'touch target').toBeGreaterThanOrEqual(44);
	expect(await page.evaluate(() => Math.max(0, document.documentElement.scrollWidth - document.documentElement.clientWidth)), 'nothing scrolls sideways').toBe(0);
});
