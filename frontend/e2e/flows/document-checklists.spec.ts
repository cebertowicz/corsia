import path from 'node:path';
import { expect, test, type APIRequestContext, type Page } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';

async function openChecklist(
	page: Page,
	request: APIRequestContext,
	text: string,
) {
	const stamp = Date.now();
	const title = `checklist-${stamp}`;
	const sessionId = await createSession(request, title);
	const token = encodeURIComponent(TEST_TOKEN);
	const treeResponse = await request.get(`/api/files/tree?token=${token}`);
	expect(treeResponse.ok()).toBeTruthy();
	const tree = await treeResponse.json();
	const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
	const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
	expect(workspaceRoot).toEqual(expect.any(String));
	const fileName = `checklist-${stamp}.md`;
	const docPath = path.join(workspaceRoot, fileName);

	const seed = await request.put(`/api/files/content?token=${token}`, {
		data: { path: docPath, text, project_id: null, thread_id: sessionId, create: true },
	});
	expect(seed.ok(), `seed failed: ${seed.status()} ${await seed.text()}`).toBeTruthy();

	await page.addInitScript(
		({ threadId, artifactPath, artifactTitle }) => {
			localStorage.setItem('corsia:artifactWorkspace:v1', JSON.stringify({
				threads: {
					[threadId]: {
						tabs: [{
							instanceId: 'checklist-tab',
							artifactId: null,
							path: artifactPath,
							title: artifactTitle,
							loadedRevision: null,
							latestRevision: null,
							status: 'loading',
						}],
						activeTabId: 'checklist-tab',
						visible: true,
						activeSurface: 'artifact',
					},
				},
				splitWidth: 420,
			}));
		},
		{ threadId: sessionId, artifactPath: docPath, artifactTitle: fileName },
	);

	await loginByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) {
		await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	}
	await page.getByText(title).first().click();
	const editor = page.locator('.cm-content');
	await expect(editor).toBeVisible();
	return { docPath, editor, sessionId, title, token };
}

async function replaceEditor(page: Page, editor: ReturnType<Page['locator']>, text: string) {
	await editor.click();
	await page.keyboard.press('ControlOrMeta+A');
	await page.keyboard.type(text);
}

test.describe('@flow:document-checklists @surface:documents GFM document checklist interactions', () => {
	test('renders, toggles, continues, saves, reloads, and keyboard-toggles GFM tasks', async ({ page, request }) => {
		test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'Desktop assertions use the split artifact leaf.');

		const fixture = await openChecklist(page, request, '- [ ] active\n- [x] done\n1. ordered\n');
		const active = page.getByRole('checkbox', { name: 'Mark task complete: active' });
		const done = page.getByRole('checkbox', { name: 'Mark task incomplete: done' });
		await expect(active).toBeVisible();
		await expect(done).toBeChecked();
		await expect(page.locator('.cm-md-bullet')).toHaveCount(0);
		await expect(fixture.editor).not.toContainText('[ ]');
		await expect(fixture.editor).not.toContainText('[x]');

		await fixture.editor.click({ position: { x: 80, y: 8 } });
		await active.click();
		await expect(active).toBeChecked();
		await expect(page.locator('.cm-md-task-done')).toBeVisible();
		await expect.poll(() => fixture.editor.evaluate((el) => document.activeElement === el)).toBe(true);
		await expect(page.getByRole('button', { name: 'Comment' })).toHaveCount(0);
		await page.keyboard.type(' continued');
		await expect(fixture.editor).toContainText('continued');

		// This is real keyboard entry into an empty document, not source seeding.
		await replaceEditor(page, fixture.editor, '- [ ] typed item');
		await expect(page.getByRole('checkbox', { name: 'Mark task complete: typed item' })).toBeVisible();

		await replaceEditor(page, fixture.editor, '- [x] checked');
		await page.keyboard.press('End');
		await page.keyboard.press('Enter');
		const tasks = page.getByRole('checkbox');
		await expect(tasks).toHaveCount(2);
		await expect(tasks.nth(1)).toHaveAccessibleName('Mark task complete: task');
		await expect(fixture.editor).not.toContainText('[ ]');

		await page.keyboard.press('ControlOrMeta+S');
		await expect(page.getByText('✓ Saved')).toBeVisible();
		const savedResponse = await request.get(
			`/api/files/content?token=${fixture.token}&path=${encodeURIComponent(fixture.docPath)}&thread_id=${fixture.sessionId}`,
		);
		expect(savedResponse.ok()).toBeTruthy();
		expect((await savedResponse.json()).text).toContain('- [x] checked\n- [ ]');

		await page.reload();
		await page.getByText(fixture.title).first().click();
		const reloadedEditor = page.locator('.cm-content');
		const reloadedTasks = page.getByRole('checkbox');
		await expect(reloadedTasks).toHaveCount(2);
		await expect(reloadedTasks.nth(0)).toHaveAccessibleName('Mark task incomplete: checked');
		await expect(reloadedTasks.nth(1)).toHaveAccessibleName('Mark task complete: task');

		await reloadedEditor.click({ position: { x: 85, y: 8 } });
		await page.keyboard.press('ControlOrMeta+Enter');
		await expect(page.getByRole('checkbox', { name: 'Mark task complete: checked' })).not.toBeChecked();
	});

	test('toggles a task by tap at mobile width', async ({ page, request }) => {
		test.skip((page.viewportSize()?.width ?? 0) > 1024, 'Mobile-specific tap assertion.');

		await openChecklist(page, request, '- [ ] mobile item\n');
		const checkbox = page.getByRole('checkbox', { name: 'Mark task complete: mobile item' });
		await checkbox.tap();
		await expect(checkbox).toBeChecked();
	});

	// The artifact leaf does not expose a separately seedable writable=false document
	// state. markdown-tasks.test.ts covers its disabled, non-toggling read-only branch.
});
