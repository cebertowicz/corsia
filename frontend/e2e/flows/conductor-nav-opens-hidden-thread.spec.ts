
import { expect, test } from '@playwright/test';
import {
	loginByUrl,
	createSession,
	waitForWsConnected,
	saveEvidenceScreenshot,
	TEST_SESSION_TOKEN,
	TEST_TOKEN,
} from '../support/corsia';

const tok = encodeURIComponent(TEST_TOKEN);
const sessionTok = encodeURIComponent(TEST_SESSION_TOKEN);

test.describe('@flow:conductor-nav @surface:navigation conductor nav opens the hidden designated thread', () => {
	test('Conductor rail item opens the designated conductor though it is absent from the sidebar', async ({ page, request }, testInfo) => {
		test.skip((page.viewportSize()?.width ?? 0) <= 1024, 'rail Conductor item is desktop-only; resolution logic is viewport-independent');

		const stamp = Date.now();
		const projName = `e2e-cond-${stamp}`;
		const condTitle = `cond-thread-${stamp}`;
		const marker = `conductor-open-marker-${stamp}`;
		const summary = 'The Conductor update remains readable in the rail';

		const presp = await request.post(`/api/projects?token=${tok}`, { data: { name: projName, status: 'active' } });
		expect(presp.ok()).toBeTruthy();
		const proj = ((await (await request.get(`/api/projects?token=${tok}&status=active`)).json()).projects || [])
			.find((p: any) => p.name === projName);
		expect(proj, 'seeded project present').toBeTruthy();

		const condId = await createSession(request, condTitle);
		expect((await request.post(`/api/threads/${condId}/system-message?token=${tok}`, { data: { content: marker } })).ok()).toBeTruthy();

		expect((await request.put(`/api/conductor?token=${sessionTok}&scope=${proj.id}`, { data: { thread_id: condId } })).ok()).toBeTruthy();

		expect((await (await request.get(`/api/conductor?token=${tok}&scope=${proj.id}`)).json()).thread_id).toBe(condId);
		expect(
			((await (await request.get(`/api/threads?token=${tok}`)).json()).threads || []).some((s: any) => s.id === condId),
			'conductor excluded from /api/threads'
		).toBeFalsy();

		await page.route('**/api/plans/operator/thread-states?ids=*', async (route) => {
			const ids = new URL(route.request().url()).searchParams.get('ids')?.split(',') ?? [];
			const states = ids.includes(condId)
				? { [condId]: {
					state: 'failed', label_key: null, paused: false,
					attention_items: [{ kind: 'failed', title: 'Prepare the release evidence', node_id: 'node-proof' }],
					latest_update: { kind: 'marked', summary }, has_update: true, opened: false,
				} }
				: {};
			await route.fulfill({ json: { states } });
		});

		await loginByUrl(page, { waitForComposer: false });

		await page.getByRole('button', { name: /Jump to/ }).click();
		await expect(page.locator('.palette')).toBeVisible();
		await page.locator('.palette').getByRole('button', { name: projName, exact: true }).click();
		await expect(page.getByRole('button', { name: 'Back to workspace' })).toBeVisible({ timeout: 10_000 });
		await expect(page.locator('.thread-item').filter({ hasText: condTitle })).toHaveCount(0);

		const conductorRow = page.locator('.scoped-item.conductor');
		const conductorButton = conductorRow.getByRole('button', { name: `Conductor. Latest update: ${summary}` });
		await expect(conductorRow.locator('.update-summary')).toHaveText(summary);
		await expect(conductorRow.locator('.update-summary')).toHaveAttribute('title', summary);
		await expect(conductorRow.locator('.plan-state.failed')).toBeVisible();
		await conductorButton.click();
		await waitForWsConnected(page);
		await expect(page.locator('.pane-scope')).toContainText(projName);

		await expect(page.getByText(marker)).toBeVisible({ timeout: 10_000 });

		await saveEvidenceScreenshot(page, testInfo, 'conductor-nav-opens-hidden-thread');
	});
});
