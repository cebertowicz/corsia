import { expect, test, type APIRequestContext } from '@playwright/test';
import { loginByUrl, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';

const tok = encodeURIComponent(TEST_TOKEN);

async function createProject(request: APIRequestContext, name: string, accent: string) {
	const response = await request.post(`/api/projects?token=${tok}`, {
		data: { name, description: 'Agent navigation browser proof', status: 'active', accent_color: accent },
	});
	expect(response.ok()).toBeTruthy();
	const projects = await (await request.get(`/api/projects?token=${tok}&status=active`)).json();
	const project = (projects.projects || []).find((candidate: { name: string }) => candidate.name === name);
	expect(project, 'created project is available to the navigation palette').toBeTruthy();
	return project as { id: string; accent_color: string };
}

async function createSession(request: APIRequestContext, title: string, projectId: string) {
	const response = await request.post(`/api/threads?token=${tok}`, {
		data: { title, project_id: projectId, model: 'mock/test-model' },
	});
	expect(response.ok()).toBeTruthy();
	const body = await response.json();
	return body.thread_id as string;
}

async function createAgent(request: APIRequestContext, title: string, projectId: string) {
	const sessionId = await createSession(request, title, projectId);
	const response = await request.patch(`/api/agents/${sessionId}?token=${tok}`, {
		data: { responsibility: `Owns ${title}`, active: true },
	});
	expect(response.ok()).toBeTruthy();
	return sessionId;
}

async function openProjectScope(page: import('@playwright/test').Page, projectName: string) {
	if (!(await page.getByRole('button', { name: /Jump to/ }).isVisible())) {
		await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	}
	await page.getByRole('button', { name: /Jump to/ }).click();
	const projectRow = page.locator('.palette .res').filter({ hasText: projectName });
	await expect(projectRow).toBeVisible({ timeout: 10_000 });
	await projectRow.click();
	await expect(page.getByRole('button', { name: 'Back to workspace' })).toBeVisible({ timeout: 10_000 });
}

test.describe('@flow:agents-nav-management @surface:navigation compact Agent drawer', () => {
	test('keeps Agents actionable and conversations reachable on desktop and mobile', async ({ page, request }, testInfo) => {
		const stamp = `${testInfo.project.name}-${Date.now()}`;
		const projectName = `Agents ${stamp}`;
		const accent = '#d65a87';
		const project = await createProject(request, projectName, accent);
		const agentIds: string[] = [];
		const agentTitles: string[] = [];

		for (let index = 1; index <= 15; index += 1) {
			const title = `Agent ${String(index).padStart(2, '0')} ${stamp}`;
			agentTitles.push(title);
			agentIds.push(await createAgent(request, title, project.id));
		}
		const conversationTitle = `Reachable conversation ${stamp}`;
		await createSession(request, conversationTitle, project.id);

		// Eight unread Agents force five into the visible budget and leave exactly
		// three unread in the ten-row overflow: "Show 10 more · 3 unread".
		await page.addInitScript((ids) => {
			localStorage.setItem('corsia_unread', JSON.stringify(ids));
		}, agentIds.slice(7));

		await loginByUrl(page);
		await openProjectScope(page, projectName);

		const agents = page.locator('.scoped-item.agent');
		await expect(agents).toHaveCount(5);
		await expect(page.locator('.scoped-item.conductor')).toBeVisible();
		await expect(page.locator('.rail-nav button').filter({ hasText: 'Documents' })).toHaveCount(0);

		const drawer = page.locator('.drawer-toggle');
		await expect(drawer).toContainText('Show 10 more');
		await expect(drawer.locator('.drawer-attention')).toHaveText('3 unread');
		const colors = await drawer.locator('.drawer-attention').evaluate((element) => {
			const root = getComputedStyle(document.documentElement);
			const probe = document.createElement('span');
			probe.style.color = root.getPropertyValue('--accent').trim();
			document.body.appendChild(probe);
			const accentColor = getComputedStyle(probe).color;
			probe.remove();
			return { attention: getComputedStyle(element).color, accent: accentColor };
		});
		expect(colors.attention).toBe(colors.accent);

		const conductorStyle = await page.locator('.scoped-item.conductor').evaluate((element) => ({
			color: getComputedStyle(element).color,
			weight: getComputedStyle(element).fontWeight,
			icon: getComputedStyle(element.querySelector('svg')!).color,
		}));
		const agentStyle = await agents.first().evaluate((element) => ({
			color: getComputedStyle(element).color,
			weight: getComputedStyle(element).fontWeight,
			icon: getComputedStyle(element.querySelector('svg')!).color,
		}));
		expect(conductorStyle).toEqual(agentStyle);

		await agents.first().click({ button: 'right' });
		const menu = page.locator('.context-menu');
		await expect(menu).toBeVisible();
		await expect(menu).toContainText('Mark read');
		await expect(menu).toContainText('Rename');
		await expect(menu).toContainText('Agent settings');
		await expect(menu).toContainText('Deactivate Agent');
		await expect(menu).not.toContainText('Archive');
		await expect(menu).not.toContainText('Delete');
		await menu.getByRole('button', { name: 'Mark read' }).click();

		await agents.nth(1).dispatchEvent('pointerdown', {
			pointerType: 'touch',
			clientX: 120,
			clientY: 180,
		});
		await page.waitForTimeout(550);
		await expect(menu).toBeVisible();
		await expect(menu.getByRole('button', { name: /Mark (read|unread)/ })).toBeVisible();
		await page.locator('.context-menu-backdrop').click({ position: { x: 2, y: 2 } });

		await saveEvidenceScreenshot(page, testInfo, 'agents-nav-collapsed');

		await drawer.click();
		await expect(agents).toHaveCount(15);
		await expect(drawer).toContainText('Show less');

		const idleAgent = agents.filter({ hasText: agentTitles[0] });
		await idleAgent.click({ button: 'right' });
		await menu.getByRole('button', { name: 'Mark unread' }).click();
		await expect(idleAgent.locator('.unread-dot')).toBeVisible();

		const scrollSurface = page.locator('.sidebar-scroll');
		await scrollSurface.evaluate((element) => { element.scrollTop = element.scrollHeight; });
		const conversation = page.locator('.thread-item').filter({ hasText: conversationTitle });
		await conversation.scrollIntoViewIfNeeded();
		await expect(conversation).toBeVisible();

		await conversation.click({ button: 'right' });
		await expect(menu.getByRole('button', { name: 'Mark unread' })).toBeVisible();
		await menu.getByRole('button', { name: 'Mark unread' }).click();
		await expect(conversation.locator('.unread-dot')).toBeVisible();

		await saveEvidenceScreenshot(page, testInfo, 'agents-nav-expanded-scroll');
	});
});
