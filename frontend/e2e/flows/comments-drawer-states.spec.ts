import fs from 'node:fs';
import path from 'node:path';
import { expect, type APIRequestContext, type Page } from '@playwright/test';
import { TEST_TOKEN } from '../support/corsia';
import { isolatedDb } from '../support/imageRegionComments';
import { comment, journey, open, seed, settled, shot, type Case, type Doc } from '../support/reviewFixtures';

/**
 * The comments drawer in each state a person can reach, on the real composed canvas: the inline drawer
 * (pane closed), the sheet contained by the document column (default layout and phone), and the phone
 * sheet. Each journey asserts its state and saves a whole-screen frame named rs-<n> for design review.
 */
const FINE_AND_PHONE: readonly Case[] = [['1440 light pane closed', 'closed', 'light'], ['393 light', 'phone', 'light'], ['393 dark', 'phone', 'dark']];
const drawerOf = (page: Page) => page.locator('[data-doc-comments-drawer]');
const pill = (page: Page) => page.locator('.comment-status');

async function reply(request: APIRequestContext, doc: Doc, parentId: string, body: string) {
	const created = await request.post(`/api/artifacts/${doc.artifactId}/comments`, { params: { token: TEST_TOKEN }, data: { body, parent_id: parentId } });
	expect(created.ok(), await created.text()).toBeTruthy();
}
async function resolve(request: APIRequestContext, commentId: string) {
	const patched = await request.patch(`/api/comments/${commentId}`, { params: { token: TEST_TOKEN }, data: { resolved: true } });
	expect(patched.ok(), await patched.text()).toBeTruthy();
}
/** A document the files API reports as not writable: a workspace bootstrap file under the workspace's context folder. */
async function seedReadOnly(request: APIRequestContext, baseTitle: string): Promise<Doc> {
	const doc = await seed(request, baseTitle);
	const roots = await request.get('/api/files/tree', { params: { token: TEST_TOKEN } });
	const workspace = (await roots.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path as string;
	const name = `${doc.name.replace(/\s+/g, '-')}`, docPath = path.join(workspace, 'context', name);
	fs.mkdirSync(path.dirname(docPath), { recursive: true });
	fs.writeFileSync(docPath, doc.text);
	const registered = await request.post('/api/files/register', { params: { token: TEST_TOKEN }, data: { path: docPath, thread_id: doc.threadId } });
	expect(registered.ok(), await registered.text()).toBeTruthy();
	return { ...doc, name, docPath, artifactId: (await registered.json()).artifact.id as string };
}
async function chooseFilter(page: Page, name: 'Open' | 'Resolved' | 'All') {
	const filter = drawerOf(page).locator('[data-drawer-filter]');
	await filter.click();
	await drawerOf(page).getByRole('menu', { name: 'Filter comments', exact: true }).getByRole('menuitemradio', { name, exact: true }).click();
	await expect(filter).toHaveAttribute('aria-label', `Filter comments: ${name}`);
}

journey('filters, replies and a jump near the end of the document', FINE_AND_PHONE, async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Drawer states ${mode} ${theme}`);
	const first = await comment(request, doc, 'Ring two should wait.', 2);
	const second = await comment(request, doc, 'Add a rollback line.', 5);
	await comment(request, doc, 'Name the owner of the last ring.', 12);
	await reply(request, doc, first, 'Agreed, ring two waits for the canary.');
	await reply(request, doc, first, 'I will move it after the canary window.');
	await resolve(request, second);
	await open(page, doc, mode, theme);
	await pill(page).click();
	const drawer = drawerOf(page);
	await expect(drawer).toBeVisible();
	await settled(page);
	await expect(drawer.locator('.drawer-heading h2')).toContainText('2 open');
	const toggle = drawer.getByRole('button', { name: /2 replies/ });
	await expect(toggle).toBeVisible();
	await shot(page, 'rs-13a-thread-replies-collapsed', info);
	await toggle.click();
	await expect(drawer.getByText('Agreed, ring two waits for the canary.')).toBeVisible();
	await settled(page);
	await shot(page, 'rs-13b-thread-replies-expanded', info);

	// The All filter counts every thread, and its menu item carries a visible check, not colour alone.
	await chooseFilter(page, 'All');
	await expect(drawer.locator('.drawer-heading h2')).toContainText('3 threads');
	await expect(drawer.locator('[data-comment-jump]')).toHaveCount(3);
	await shot(page, 'rs-14-filter-all-with-count', info);
	await drawer.locator('[data-drawer-filter]').click();
	const menu = drawer.getByRole('menu', { name: 'Filter comments', exact: true });
	await expect(menu.getByRole('menuitemradio', { checked: true })).toHaveCount(1);
	await expect(menu.getByRole('menuitemradio', { name: 'All', exact: true }).locator('.filter-check svg')).toHaveCount(1);
	await expect(menu.getByRole('menuitemradio', { name: 'Open', exact: true }).locator('.filter-check svg')).toHaveCount(0);
	await shot(page, 'rs-15-filter-menu-check', info);
	await menu.getByRole('menuitemradio', { name: 'Open', exact: true }).click();
	await expect(drawer.locator('[data-drawer-filter]')).toHaveAttribute('aria-label', 'Filter comments: Open');

	// A reply opens in place; on a fine pointer the Reply text opens the field, on touch the icon does.
	const firstCard = drawer.getByRole('region', { name: /^Comment by / }).first();
	await firstCard.getByRole('button', { name: 'Reply', exact: true }).first().click();
	const field = firstCard.locator('.ct-input');
	await expect(field).toBeVisible();
	await expect(field).toBeFocused();
	await expect(field).toBeInViewport();
	await shot(page, mode === 'phone' ? 'rs-16-reply-open-touch' : 'rs-16-reply-open-fine-pointer', info);

	// A quote near the end of the document scrolls the document under the drawer and keeps that thread's Reply in view.
	const last = drawer.locator('[data-comment-jump]').last();
	await last.click();
	await settled(page);
	await expect(page.locator('.cm-content')).toContainText('Step 12:');
	await expect(drawer).toBeVisible();
	await shot(page, 'rs-17-quote-jump-near-document-end', info);
});

journey('a long author name never breaks the row', [['1440 light pane closed', 'closed', 'light'], ['393 light', 'phone', 'light']], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Drawer long name ${mode} ${theme}`);
	const id = await comment(request, doc, 'Please confirm the rollback owner before the final ring.', 3);
	isolatedDb([
		'changed = connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Platform Reliability Release Coordination Agent", sys.argv[2], sys.argv[3])).rowcount',
		'assert changed == 1, changed',
	], doc.threadId, id);
	await open(page, doc, mode, theme);
	await pill(page).click();
	const drawer = drawerOf(page);
	await expect(drawer.getByRole('region', { name: /^Comment by Platform Reliability/ })).toBeVisible();
	await settled(page);
	const row = drawer.getByRole('region', { name: /^Comment by / }).first();
	const overflow = await row.evaluate((el) => {
		const card = el.getBoundingClientRect();
		const controls = [...el.querySelectorAll<HTMLElement>('.ct-act, .ct-time, [data-comment-time]')];
		return { scrollW: el.scrollWidth, clientW: el.clientWidth, outside: controls.filter((c) => c.getBoundingClientRect().right > card.right + 0.5).length };
	});
	expect(overflow.scrollW, 'no sideways overflow').toBeLessThanOrEqual(overflow.clientW + 1);
	expect(overflow.outside, 'every row control stays inside the card').toBe(0);
	await shot(page, 'rs-18-long-author-name', info);
});

journey('the empty and the resolved-empty drawer', FINE_AND_PHONE, async ({ page, request, info, mode, theme }) => {
	const empty = await seed(request, `Drawer empty ${mode} ${theme}`);
	await open(page, empty, mode, theme);
	await expect(pill(page)).toBeEnabled();
	await pill(page).click();
	await expect(drawerOf(page).getByText('No comments yet.')).toBeVisible();
	await settled(page);
	await shot(page, 'rs-19-empty', info);
});

journey('Resolved with nothing resolved offers Show open', [['1440 light pane closed', 'closed', 'light'], ['393 light', 'phone', 'light']], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Drawer resolved empty ${mode} ${theme}`);
	await comment(request, doc, 'Ring two should wait.', 2);
	await comment(request, doc, 'Add a rollback line.', 5);
	await open(page, doc, mode, theme);
	await pill(page).click();
	const drawer = drawerOf(page);
	await chooseFilter(page, 'Resolved');
	await expect(drawer.getByText('No resolved comments.')).toBeVisible();
	const showOpen = drawer.getByRole('button', { name: 'Show open · 2' });
	await expect(showOpen).toBeVisible();
	await settled(page);
	await shot(page, 'rs-20-resolved-empty-show-open', info);
	await showOpen.click();
	await expect(drawer.locator('[data-drawer-filter]')).toHaveAttribute('aria-label', 'Filter comments: Open');
	await expect(drawer.locator('[data-comment-jump]')).toHaveCount(2);
});

journey('a read-only document lists comments without writing controls', [['1440 light pane closed', 'closed', 'light'], ['393 light', 'phone', 'light']], async ({ page, request, info, mode, theme }) => {
	const doc = await seedReadOnly(request, `Drawer read only ${mode} ${theme}`);
	const first = await comment(request, doc, 'Ring two should wait.', 2);
	await reply(request, doc, first, 'Agreed, ring two waits for the canary.');
	await open(page, doc, mode, theme);
	await pill(page).click();
	const drawer = drawerOf(page);
	await drawer.getByRole('button', { name: /1 reply/ }).click();
	await expect(drawer.getByText('Agreed, ring two waits for the canary.')).toBeVisible();
	await expect(drawer.locator('.ct-act:visible'), 'no reply, agree or resolve control').toHaveCount(0);
	await expect(drawer.locator('.ct-compose:visible')).toHaveCount(0);
	await settled(page);
	await shot(page, 'rs-21-read-only', info);
});

journey('a failed refresh offers Retry, and Retry reads as Loading', [['1440 light pane closed', 'closed', 'light'], ['393 light', 'phone', 'light']], async ({ page, request, info, mode, theme }) => {
	const doc = await seed(request, `Drawer loading ${mode} ${theme}`);
	const first = await comment(request, doc, 'Ring two should wait.', 2);
	await open(page, doc, mode, theme);
	await pill(page).click();
	const drawer = drawerOf(page);
	await expect(drawer).toBeVisible();
	// Refreshes of comments already shown stay quiet; only a failed read followed by Retry reads as Loading.
	let reads: 'pass' | 'fail' | 'hold' = 'pass';
	let release!: () => void;
	const held = new Promise<void>((resolveHold) => { release = resolveHold; });
	await page.route(`**/api/artifacts/${doc.artifactId}/comments*`, async (route) => {
		if (route.request().method() !== 'GET' || reads === 'pass') return route.continue();
		if (reads === 'fail') return route.abort('failed');
		await held;
		return route.continue();
	});
	try {
		reads = 'fail';
		await reply(request, doc, first, 'Agreed, ring two waits for the canary.');
		await expect(drawer.getByText(/Couldn't load comments/)).toBeVisible({ timeout: 15_000 });
		await settled(page);
		await shot(page, 'rs-22a-load-failed-retry', info);
		reads = 'hold';
		await drawer.getByRole('button', { name: 'Retry', exact: true }).click();
		await expect(drawer.getByText('Loading comments…')).toBeVisible();
		await settled(page);
		await shot(page, 'rs-22b-loading', info);
	} finally {
		release();
	}
	await expect(drawer.getByText('Loading comments…')).toBeHidden();
	await expect(drawer.getByRole('button', { name: /1 reply/ })).toBeVisible();
});
