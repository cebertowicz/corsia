import path from 'node:path';
import { expect } from '@playwright/test';
import { test } from '../support/test';
import { e2eModel, loginByUrl, TEST_TOKEN } from '../support/corsia';

test('a project landing opens its documents library without a chat', async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop');
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
	await page.getByRole('button', { name: /Jump to/ }).click();
	await page.locator('.palette').getByRole('button', { name, exact: true }).click();
	await page.getByRole('button', { name: 'New chat' }).first().click();
	await expect(page.getByText('No documents yet')).toHaveCount(0);
	await page.getByRole('button', { name: 'Browse documents…' }).click();
	await expect(page.locator('.docs')).toBeVisible();
});
