import path from 'node:path';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { e2eModel, loginByUrl, TEST_TOKEN } from '../support/corsia';

test('a project landing opens its documents library without a chat', async ({ page, request }, info) => {
	test.skip(!['chromium-desktop', 'chromium-mobile'].includes(info.project.name));
	const phone = info.project.name === 'chromium-mobile';
	if (phone) await page.setViewportSize({ width: 393, height: 852 });
	const params = { token: TEST_TOKEN };
	const name = `Project documents ${Date.now()}`;
	const created = await request.post('/api/projects', { params, data: { name, status: 'active' } });
	expect(created.ok()).toBeTruthy();
	const list = await (await request.get('/api/projects', { params: { ...params, status: 'active' } })).json();
	const project = list.projects.find((item: { name: string }) => item.name === name);
	expect(project).toBeTruthy();
	const thread = await request.post('/api/threads', { params, data: { title: 'Fixture', project_id: project.id, model: e2eModel() } });
	expect(thread.ok()).toBeTruthy();
	const threadId = (await thread.json()).thread_id;
	const doc = await request.put('/api/files/content', { params, data: {
		path: path.join(project.path, 'docs', 'garden.md'), text: '# Garden\n', thread_id: threadId, create: true,
	} });
	expect(doc.ok()).toBeTruthy();

	await loginByUrl(page, { waitForComposer: false });
	if (phone) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByRole('button', { name: /Jump to/ }).click();
	await page.locator('.palette').getByRole('button', { name, exact: true }).click();
	await expect(page.locator('.palette')).toBeHidden();
	const newChat = page.locator('.rail-newchat-wrap').getByRole('button', { name: 'New chat', exact: true });
	if (phone && !(await newChat.isVisible())) {
		await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	}
	await newChat.click();
	if (phone) await page.getByRole('button', { name: 'Toggle panel' }).click();
	await expect(page.getByText('No documents yet')).toHaveCount(0);
	await page.getByRole('button', { name: 'Browse documents…' }).click();
	await expect(page.locator('.docs')).toBeVisible();
	if (phone) await expect(page.locator('aside.pane')).not.toHaveClass(/\bopen\b/);
});
