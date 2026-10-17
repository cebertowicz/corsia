import { expect, test } from '@playwright/test';
import { loginByUrl, saveEvidenceScreenshot, TEST_SESSION_TOKEN, TEST_TOKEN } from '../support/corsia';

const tok = encodeURIComponent(TEST_TOKEN);
const sessionTok = encodeURIComponent(TEST_SESSION_TOKEN);

test('@surface:navigation project and conductor settings use their own header gear', async ({ page, request }, testInfo) => {
	const name = `settings-header-${Date.now()}`;
	const projectResponse = await request.post(`/api/projects?token=${tok}`, {
		data: { name, description: 'Settings navigation proof', status: 'active' },
	});
	expect(projectResponse.ok()).toBe(true);
	const projects = await (await request.get(`/api/projects?token=${tok}&status=active`)).json();
	const project = projects.projects.find((candidate: { name: string }) => candidate.name === name);
	expect(project).toBeTruthy();

	const sessionResponse = await request.post(`/api/threads?token=${tok}`, {
		data: { title: 'Designated conductor', project_id: project.id, model: 'mock/test-model' },
	});
	expect(sessionResponse.ok()).toBe(true);
	const { thread_id: conductorId } = await sessionResponse.json();
	expect((await request.put(`/api/conductor?token=${sessionTok}&scope=${project.id}`, {
		data: { thread_id: conductorId },
	})).ok()).toBe(true);

	await loginByUrl(page);
	for (const width of [1280, 393]) {
		await page.setViewportSize({ width, height: 800 });
		const jump = page.getByRole('button', { name: /Jump to/ });
		if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await jump.click();
		await page.locator('.palette').getByRole('button', { name, exact: true }).click();
		await expect(page.getByRole('button', { name: 'Project settings' }).first()).toBeVisible();
		await page.locator('.shell-header').getByRole('button', { name: 'Project settings' }).click();
		await expect(page.getByRole('textbox', { name: 'Name' })).toHaveValue(name);
		await saveEvidenceScreenshot(page, testInfo, `project-settings-${width}`);
		await page.locator('.shell-header .h-back').click();
		const conductor = page.locator('.scoped-item.conductor');
		if (width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await conductor.click();
		await expect(page.locator('.shell-header').getByRole('button', { name: 'Conductor settings' })).toBeVisible();
		await page.locator('.shell-header').getByRole('button', { name: 'Conductor settings' }).click();
		await expect(page.locator('.shell-header')).toContainText('Conductor settings');
		await expect(page.getByText('Thread skills', { exact: true })).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, `conductor-settings-${width}`);
		await page.locator('.shell-header .h-back').click();
	}
});
