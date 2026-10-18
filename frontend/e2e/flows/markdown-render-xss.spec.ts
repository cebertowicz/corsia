import { expect, test } from '@playwright/test';
import { captureConsoleErrors, createSession, openAppPastLanding, TEST_TOKEN, waitForWsConnected, watchSessionSubscriptions, sidebarThread } from '../support/corsia';
import { setFocusMode } from '../support/focusMode';
import { injectMessage } from '../support/mock';

// In a real browser an un-stripped onerror fires and sets the sentinel; renderMarkdown
// (the choke point) sanitizes, so it never fires. The chat path exercises the same
// `{@html renderMarkdown(...)}` rendering used by tool-activity and the conversation viewer.
const PAYLOAD = 'before <img src=x onerror="window.__xssFired = true"> <script>window.__xssFired = true</script> after';

function sentinel(page: import('@playwright/test').Page) {
	return page.evaluate(() => (window as unknown as { __xssFired?: boolean }).__xssFired);
}
function appErrors(errs: string[]) {
	// the sanitized <img> keeps a harmless dummy src that 404s — not an app error
	return errs.filter((e) => !/Failed to load resource/i.test(e));
}

test.describe('@flow:markdown-xss @surface:chat renderMarkdown sanitizes chat messages', () => {
	// Focus is on by default and hides the assistant message body; these specs cover the rendered reply.
	test.beforeEach(async ({ request }) => {
		await setFocusMode(request, false);
	});
	test.afterEach(async ({ request }) => {
		await setFocusMode(request, true);
	});

	test('an injected assistant message with script/handler payload is neutralized', async ({ page, request }) => {
		const errors = captureConsoleErrors(page);
		const subscriptions = watchSessionSubscriptions(page);
		const title = `e2e md-xss chat ${Date.now()}`;
		const sessionId = await createSession(request, title);

		await openAppPastLanding(page);
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await sidebarThread(page, title).click({ timeout: 10_000 });
		await waitForWsConnected(page);
		await subscriptions.waitForSubscribed(sessionId);

		await injectMessage(request, sessionId, { role: 'assistant', content: PAYLOAD }, TEST_TOKEN);

		const body = page.locator('.assistant-msg .md-content').first();
		await expect(body).toBeVisible({ timeout: 10_000 });
		await expect(body).toContainText('after');
		await page.waitForTimeout(300);
		expect(await sentinel(page)).toBeFalsy();
		const html = (await body.evaluate((el) => el.innerHTML)).toLowerCase();
		expect(html).not.toContain('<script');
		expect(html).not.toContain('onerror');
		expect(appErrors(errors), appErrors(errors).join('\n')).toEqual([]);
	});
});

test.describe('@flow:markdown-copy @surface:chat code-block copy still works after sanitization', () => {
	// Focus is on by default and hides the assistant message body; these specs cover the rendered reply.
	test.beforeEach(async ({ request }) => {
		await setFocusMode(request, false);
	});
	test.afterEach(async ({ request }) => {
		await setFocusMode(request, true);
	});

	test('the delegated copy button copies the code (the inline onclick was removed)', async ({ page, request, context }) => {
		await context.grantPermissions(['clipboard-read', 'clipboard-write']);
		const subscriptions = watchSessionSubscriptions(page);
		const title = `e2e md-copy ${Date.now()}`;
		const sessionId = await createSession(request, title);

		await openAppPastLanding(page);
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await sidebarThread(page, title).click({ timeout: 10_000 });
		await waitForWsConnected(page);
		await subscriptions.waitForSubscribed(sessionId);

		await injectMessage(request, sessionId, { role: 'assistant', content: '```js\nconst answer = 42;\n```' }, TEST_TOKEN);

		const copyBtn = page.locator('.assistant-msg .copy-btn').first();
		await expect(copyBtn).toBeVisible({ timeout: 10_000 });
		await copyBtn.click();
		await expect(copyBtn).toHaveText('Copied!', { timeout: 5_000 });
		const clip = await page.evaluate(() => navigator.clipboard.readText());
		expect(clip).toContain('const answer = 42;');
	});
});

test.describe('@flow:markdown-xss @surface:documents renderMarkdown sanitizes the doc table widget', () => {
	const DOC_PATH = 'e2e-md-xss-table.md';
	const DOC_TEXT = [
		'# md-xss',
		'',
		'| Item | Payload |',
		'|------|---------|',
		'| ok | <img src=x onerror="window.__xssFired = true"> |',
		'| <script>window.__xssFired = true</script> | done |',
		''
	].join('\n');

	test('a malicious table in a document renders sanitized in the viewer', async ({ page, request }) => {
		const errors = captureConsoleErrors(page);
		const tok = encodeURIComponent(TEST_TOKEN);
		const title = `e2e md-xss document ${Date.now()}`;
		const sessionId = await createSession(request, title);

		const tree = await (await request.get(`/api/files/tree?token=${tok}`)).json();
		const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
		const wsRoot = (roots.find((r) => r.kind === 'workspace') ?? roots[0]).path;
		const absPath = `${wsRoot}/${DOC_PATH}`;
		let seed = await request.put(`/api/files/content?token=${tok}`, {
			data: { path: absPath, text: DOC_TEXT, project_id: null, thread_id: sessionId, create: true }
		});
		if (!seed.ok() && seed.status() === 409) {
			seed = await request.put(`/api/files/content?token=${tok}`, {
				data: { path: absPath, text: DOC_TEXT, project_id: null, thread_id: sessionId, create: false }
			});
		}
		expect(seed.ok(), `seed failed: ${seed.status()} ${await seed.text()}`).toBeTruthy();

		await openAppPastLanding(page);
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await sidebarThread(page, title).click({ timeout: 10_000 });
		const browseDocuments = page.getByRole('button', { name: 'Browse documents…' });
		if (!(await browseDocuments.isVisible())) {
			await page.getByRole('button', { name: 'Toggle panel' }).click();
		}
		await browseDocuments.evaluate((button) => button.click());
		await page.getByPlaceholder('Search docs…').fill('e2e-md-xss-table');
		await page.getByText(DOC_PATH).first().click({ timeout: 10_000 });
		await page.getByRole('button', { name: 'Open document' }).evaluate((button) => button.click());

		const table = page.locator('.cm-md-table table').first();
		await expect(table).toBeVisible({ timeout: 10_000 });
		await expect(table).toContainText('done');
		await page.waitForTimeout(300);
		expect(await sentinel(page)).toBeFalsy();
		const html = (await table.evaluate((el) => el.innerHTML)).toLowerCase();
		expect(html).not.toContain('<script');
		expect(html).not.toContain('onerror');
		expect(appErrors(errors), appErrors(errors).join('\n')).toEqual([]);
	});
});
