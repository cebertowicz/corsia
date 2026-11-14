// Email draft: the comment jump control follows the body that scrolls (#2483). A draft lets its CodeMirror scroller grow to the whole
// text and scrolls `.doc-body.email-scroll` around it, so the control has to judge a highlight against that body.
import { createHash } from 'node:crypto';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { createSession, loginByUrl, sidebarThread, TEST_TOKEN } from '../support/corsia';
import { fresh, markEmailDraft, mockEmailDraftContext } from '../support/emailDraftContext';
import { isolatedDb } from '../support/heldComments';

const RUN = `${Date.now().toString(36)}-${Math.random().toString(36).slice(2, 6)}`;
const ANCHOR = 'The north trailhead lot is where the crew meets at eight.';
const PARAGRAPHS = Array.from({ length: 40 }, (_, i) => i === 19
	? `${ANCHOR} Please confirm that you can be there.`
	: `Paragraph ${i + 1} of the brush crew plan: the loppers, the sign-in sheet and the water cooler all ride in the same truck, and the drainage cut is walked before anyone else arrives.`);
const DOC = ['From: Alex Rivera <alex.rivera@example.com>', 'To: marcus.obi@example.com', 'Subject: Brush crew plan for Saturday', '', ...PARAGRAPHS.flatMap((p) => [p, '']), 'Thanks,', 'Alex', ''].join('\n');

const control = (page: Page) => page.locator('[data-doc-jump]');
const scrollBody = (page: Page, to: 'top' | 'end' | 'anchor') => page.locator('.email-scroll').evaluate((el, to) => {
	if (to === 'anchor') document.querySelector('[data-comment-ids]')!.scrollIntoView({ block: 'center' });
	else el.scrollTop = to === 'top' ? 0 : el.scrollHeight;
}, to);

test('@surface:doc-jump-control email draft: the control shows for a highlight scrolled out of view, above and below, and not on screen', async ({ page, request }, testInfo) => {
	test.setTimeout(120_000);
	test.skip(process.env.CORSIA_E2E_MOCK !== '1' || testInfo.project.name !== 'chromium-desktop', 'Requires the disposable mock instance, at the desktop project\'s 1440 width');
	const title = `Email jump ${RUN}`;
	const threadId = await createSession(request, title);
	const tree = await request.get('/api/files/tree', { params: { token: TEST_TOKEN } });
	const workspace = ((await tree.json()).roots as Array<{ kind: string; path: string }>).find((root) => root.kind === 'workspace')!.path;
	const name = `email-${createHash('md5').update(RUN).digest('hex').slice(0, 12)}.md`;
	const docPath = path.join(workspace, 'email-drafts', name);
	mkdirSync(path.dirname(docPath), { recursive: true });
	const saved = await request.put('/api/files/content', { params: { token: TEST_TOKEN }, data: { path: docPath, text: DOC, thread_id: threadId, create: true } });
	expect(saved.ok(), await saved.text()).toBeTruthy();
	const artifactId = (await saved.json()).artifact.id as string;
	const from = DOC.indexOf(ANCHOR);
	expect(from).toBeGreaterThan(-1);
	const root = await request.post(`/api/artifacts/${artifactId}/comments`, {
		params: { token: TEST_TOKEN },
		data: { body: 'Is eight early enough?', anchor_text: ANCHOR, from_off: from, to_off: from + ANCHOR.length, delivery_policy: 'immediate' },
	});
	expect(root.ok(), await root.text()).toBeTruthy();
	const rootId = (await root.json()).comment.id as string;
	// An agent's answer on the reader's comment is what makes it unread.
	const now = new Date().toISOString();
	isolatedDb([
		"connection.execute(\"INSERT INTO doc_comments (id, artifact_id, parent_id, author, author_kind, author_thread_id, body, created_at, updated_at) VALUES (lower(hex(randomblob(16))), ?, ?, 'Maya Chen', 'agent', ?, 'Eight works.', ?, ?)\", sys.argv[2:7])",
	], artifactId, rootId, threadId, now, now);

	await mockEmailDraftContext(page, () => fresh());
	await markEmailDraft(page, docPath);
	await page.addInitScript(({ threadId, docPath, name }) => localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
		threads: { [threadId]: { tabs: [{ instanceId: 'email-jump', artifactId: null, path: docPath, title: name, loadedRevision: null, latestRevision: null, status: 'loading' }], activeTabId: 'email-jump', visible: true, activeSurface: 'artifact' } },
		splitWidth: 640,
	})), { threadId, docPath, name });
	await loginByUrl(page);
	await sidebarThread(page, title).click();
	await expect(page.locator('.cm-content')).toContainText('brush crew plan');

	// The body is what scrolls, and the editor inside it is as tall as the whole text.
	const geometry = await page.evaluate(() => {
		const body = document.querySelector<HTMLElement>('.email-scroll')!;
		const scroller = document.querySelector<HTMLElement>('.email-scroll .cm-scroller')!;
		return { bodyOverflowY: getComputedStyle(body).overflowY, scrollerOverflowY: getComputedStyle(scroller).overflowY, bodyScrolls: body.scrollHeight > body.clientHeight + 100, scrollerHeight: scroller.getBoundingClientRect().height, bodyHeight: body.getBoundingClientRect().height };
	});
	expect(geometry).toMatchObject({ bodyOverflowY: 'auto', scrollerOverflowY: 'visible', bodyScrolls: true });
	expect(geometry.scrollerHeight).toBeGreaterThan(geometry.bodyHeight);

	await scrollBody(page, 'top');
	await expect(control(page).getByRole('button')).toHaveAccessibleName(/^1 new, next below: .*north trailhead lot/);
	await expect(control(page).getByRole('button')).toContainText('↓');

	await scrollBody(page, 'end');
	await expect(control(page).getByRole('button')).toHaveAccessibleName(/^1 new, next above: .*north trailhead lot/);
	await expect(control(page).getByRole('button')).toContainText('↑');

	await scrollBody(page, 'anchor');
	await expect(control(page)).toHaveCount(0);

	// Scrolling away again brings it back, so the control follows the reader both ways.
	await scrollBody(page, 'top');
	await expect(control(page).getByRole('button')).toHaveAccessibleName(/^1 new, next below: /);
});
