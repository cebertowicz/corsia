/**
 * Activity's open item shares the app header's band with the header's own right controls: the item's tab, Comments and
 * Details sit in the pane's strip, which must stop short of the header's settings gear by the width those controls
 * measure. The person's report (a document open in Activity, the gear drawn over Details) is that clearance missing,
 * so each control is measured against the gear here, and pressed, at desktop and phone width.
 */
import path from 'node:path';
import type { APIRequestContext, Page } from '@playwright/test';
import { test, expect } from './support/test';
import { createSession, openAppPastLanding, TEST_TOKEN } from './support/corsia';
import { isolatedDb } from './support/imageRegionComments';

const OUT = process.env.ACTIVITY_OUT;
const tok = () => encodeURIComponent(TEST_TOKEN);

test.use({ timezoneId: 'America/Los_Angeles', locale: 'en-US', colorScheme: 'light', seedPerTest: true });
test.beforeEach(({}, info) => test.skip(info.project.name === 'chromium-mobile', 'each test sets its own viewport'));

interface Box { x: number; y: number; width: number; height: number }
interface Measure {
	gear: Box | null;
	comments: Box | null;
	details: Box | null;
	strip: Box | null;
	headerControls: Box | null;
	inset: string | null;
}

interface World { thread: string; label: string; artifactId: string; now: number; rankedAt: string }

/** One thread, one document, and one unread agent comment on it: the person's item, made for real. */
async function seedWorld(request: APIRequestContext): Promise<World> {
	const thread = await createSession(request, 'Activity header clearance');
	const tree = await request.get(`/api/files/tree?token=${tok()}`);
	expect(tree.ok(), await tree.text()).toBeTruthy();
	const roots: Array<{ kind: string; path: string }> = (await tree.json()).roots;
	const root = roots.find((r) => r.kind === 'workspace')?.path;
	expect(root).toEqual(expect.any(String));
	const name = `before-after-${Date.now().toString(36)}.md`;
	const text = '# Before and after\n\nThe header band before the fix, and after it.\n';
	const created = await request.put(`/api/files/content?token=${tok()}`, {
		data: { path: path.join(root!, name), text, thread_id: thread, create: true },
	});
	expect(created.ok(), await created.text()).toBeTruthy();
	const artifactId = (await created.json()).artifact.id as string;

	const from = text.indexOf('after the fix');
	const commented = await request.post(`/api/artifacts/${artifactId}/comments?token=${tok()}`, {
		data: { body: 'The gear sits on Details here.', anchor_text: 'after the fix', from_off: from, to_off: from + 13, delivery_policy: 'immediate' },
	});
	expect(commented.ok(), await commented.text()).toBeTruthy();
	const commentId = (await commented.json()).comment.id as string;
	// An agent's comment is the unread one; the person's own is never counted as waiting for them.
	isolatedDb([
		'changed = connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", ("Activity header clearance", sys.argv[2], sys.argv[3])).rowcount',
		'assert changed == 1, changed',
	], thread, commentId);

	const now = Date.now();
	const ranked = new Date(now - 60_000);
	return { thread, label: 'Activity header clearance', artifactId, now, rankedAt: ranked.toISOString() };
}

/** The same item inside a project, which is where the person saw it: a project's header band carries only the gear. */
async function seedProjectWorld(request: APIRequestContext): Promise<World & { name: string }> {
	const name = `Activity header ${Date.now().toString(36)}`;
	const created = await request.post(`/api/projects?token=${tok()}`, { data: { name, status: 'active' } });
	expect(created.ok(), await created.text()).toBeTruthy();
	const list = await request.get(`/api/projects?status=active&token=${tok()}`);
	const project = (await list.json()).projects.find((item: { name: string }) => item.name === name);
	expect(project).toBeTruthy();
	const label = 'Activity header agent';
	const thread = (await (await request.post(`/api/threads?token=${tok()}`, {
		data: { title: label, project_id: project.id, model: 'mock/test-model' },
	})).json()).thread_id as string;
	const text = '# Before and after\n\nThe header band before the fix, and after it.\n';
	const doc = await request.put(`/api/files/content?token=${tok()}`, {
		data: { path: path.join(project.path, `before-after-${Date.now().toString(36)}.md`), text, thread_id: thread, create: true },
	});
	expect(doc.ok(), await doc.text()).toBeTruthy();
	const artifactId = (await doc.json()).artifact.id as string;
	const from = text.indexOf('after the fix');
	const commented = await request.post(`/api/artifacts/${artifactId}/comments?token=${tok()}`, {
		data: { body: 'The gear sits on Details here.', anchor_text: 'after the fix', from_off: from, to_off: from + 13, delivery_policy: 'immediate' },
	});
	const commentId = (await commented.json()).comment.id as string;
	isolatedDb([
		'changed = connection.execute("UPDATE doc_comments SET author=?, author_kind=\'agent\', author_thread_id=? WHERE id=?", (sys.argv[4], sys.argv[2], sys.argv[3])).rowcount',
		'assert changed == 1, changed',
	], thread, commentId, label);
	const now = Date.now();
	return { thread, label, name, artifactId, now, rankedAt: new Date(now - 60_000).toISOString() };
}

/** The one review item that opens the seeded document. */
function reviewItem(w: World) {
	return {
		key: 'review:before-after', external_id: 'review:before-after', renderer: 'review', kind: 'review',
		title: 'Approve the before and after frames for the header band.',
		raised_at: new Date(w.now - 3_600_000).toISOString(),
		source: { thread_id: w.thread, thread_label: w.label, project_id: null, project_label: null, parent_reviewed: true },
		open: { artifact_id: w.artifactId },
		rank: { position: 1, reason: 'It is the frame you asked for.', ranked_by: w.thread, ranked_at: w.rankedAt },
		detail: { artifact_id: w.artifactId },
	};
}

/** Activity's own read, so the item is the seeded review; the rest of the app keeps reading the real server. */
async function serveActivity(page: Page, w: World) {
	const items = [reviewItem(w)];
	await page.route(/\/api\/attention\/queue\?(.*&)?view=activity/, (route) => route.fulfill({
		status: 200, contentType: 'application/json',
		body: JSON.stringify({ count: items.length, items, ranker: { thread_id: w.thread, label: w.label } }),
	}));
}

/** The header's gear, the document's Comments and Details, and the room the strip was given. */
async function measure(page: Page): Promise<Measure> {
	return page.evaluate(() => {
		const box = (selector: string): Box | null => {
			const el = document.querySelector(selector);
			if (!el) return null;
			const r = el.getBoundingClientRect();
			return { x: r.x, y: r.y, width: r.width, height: r.height };
		};
		const main = document.querySelector('.main');
		return {
			gear: box('[aria-label="Project settings"], [aria-label="Workspace settings"]'),
			comments: box('[data-comments-entry]'),
			details: box('[data-document-details]'),
			strip: box('.canvas-header-strip, .panel-strip'),
			headerControls: box('.shell-header .h-right'),
			inset: main ? getComputedStyle(main).getPropertyValue('--header-right-controls-inset') : null,
		};
	});
}

const overlaps = (a: Box, b: Box) => a.x < b.x + b.width && b.x < a.x + a.width && a.y < b.y + b.height && b.y < a.y + a.height;

/** The open item's document, with its Comments and Details drawn. */
async function openDocument(page: Page, w: World) {
	await serveActivity(page, w);
	await openAppPastLanding(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
	await expect(page.locator('[data-activity-row]')).toHaveCount(1);
	if ((page.viewportSize()?.width ?? 0) <= 768) await page.locator('[data-activity-row]').first().click();
	await expect(page.locator('[data-document-details]')).toBeVisible({ timeout: 15_000 });
	await expect(page.locator('[data-comments-entry]')).toBeVisible();
}

async function report(page: Page, name: string) {
	if (!OUT) return;
	await page.evaluate(() => document.fonts.ready);
	await page.waitForTimeout(400);
	await page.screenshot({ path: path.join(OUT, name), animations: 'disabled' });
}

/** Nothing in the item's band sits under the gear, and each control takes the press: a control drawn under another never receives it. */
async function expectClear(page: Page, m: Measure, where: string) {
	console.log(`activity header clearance ${where} ${JSON.stringify(m)}`);
	expect(m.gear, `no settings gear ${where}`).not.toBeNull();
	expect(m.details, `no Details ${where}`).not.toBeNull();
	expect(overlaps(m.gear!, m.details!), `the gear overlaps Details ${where}: ${JSON.stringify(m)}`).toBe(false);
	expect(overlaps(m.gear!, m.comments!), `the gear overlaps Comments ${where}: ${JSON.stringify(m)}`).toBe(false);
	await page.locator('[aria-label="Project settings"], [aria-label="Workspace settings"]').click({ trial: true });
	await page.locator('[data-document-details]').click();
	await expect(page.getByRole('region', { name: 'Document details' })).toBeVisible();
	await page.locator('[data-document-details]').click();
}

test('the header gear stays clear of the document Details in Activity', async ({ page, request }) => {
	test.setTimeout(180_000);
	const w = await seedWorld(request);

	await page.setViewportSize({ width: 1440, height: 900 });
	await openDocument(page, w);
	await expectClear(page, await measure(page), '@1440');
	await report(page, 'clearance-1440.png');

	await page.setViewportSize({ width: 393, height: 852 });
	await page.reload();
	await openDocument(page, w);
	await expectClear(page, await measure(page), '@393');
	await report(page, 'clearance-393.png');
});

test('the header gear stays clear of the document Details inside a project, at the reported window', async ({ page, request }) => {
	test.setTimeout(180_000);
	const w = await seedProjectWorld(request);

	// The person's window measured 2056 CSS px, and a project's header band carries the gear alone.
	await page.setViewportSize({ width: 2056, height: 1175 });
	await serveActivity(page, w);
	await openAppPastLanding(page);
	await page.getByRole('button', { name: /Jump to/ }).click();
	await page.locator('.palette').getByRole('button', { name: w.name, exact: true }).click();
	await expect(page.locator('.palette')).toBeHidden();
	await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
	await expect(page.locator('[data-activity-row]')).toHaveCount(1);
	await expect(page.locator('[data-document-details]')).toBeVisible({ timeout: 15_000 });
	await expect(page.locator('[data-comments-entry]')).toBeVisible();
	await expectClear(page, await measure(page), '@2056 in a project');
	await report(page, 'clearance-2056-project.png');
});
