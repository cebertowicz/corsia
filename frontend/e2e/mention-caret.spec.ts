import { test } from './support/test';
import { expect } from '@playwright/test';
import { seed, open, comment } from './support/reviewFixtures';
import { beginComment } from './support/liveCommentProof';

const agents = [{ id: 'a', name: 'Alpha Agent', responsibility: 'Review work', requested: false, unavailable_reason: null, unavailable_text: null }];

for (const host of ['comment', 'reply'] as const) {
	for (const reduced of [false, true]) {
		test(`Retry and Escape preserve ${host} caret with ${reduced ? 'reduced' : 'normal'} motion`, async ({ page, request }, info) => {
			test.setTimeout(60_000);
			await page.emulateMedia({ reducedMotion: reduced ? 'reduce' : 'no-preference' });
			const doc = await seed(request, `mention caret ${host}`);
			if (host === 'reply') await comment(request, doc, 'Existing thread', 2);
			let fail = true;
			await page.route(`**/api/artifacts/${doc.artifactId}/reviews**`, (route) => fail
				? route.fulfill({ status: 503, json: {} })
				: route.fulfill({ json: { reviews: [], reviewers: agents } }));
			await open(page, doc, info.project.name.includes('mobile') ? 'phone' : 'closed', 'light');
			if (host === 'comment') await beginComment(page, 'Step 1: the rollout proceeds to the next ring.');
			else await page.getByRole('button', { name: /open thread/ }).click();
			const input = host === 'comment'
				? page.locator('textarea[placeholder="Comment, or @ to tag an agent"]:not([inert] *)')
				: page.locator('.ct:not([inert] *)').filter({ hasText: 'Existing thread' }).first().locator('textarea');
			await input.fill('Hello @Al');
			await expect(page.getByText("Couldn't load agents.")).toBeVisible();
			const caret = await input.evaluate((element: HTMLTextAreaElement) => element.selectionStart);
			fail = false;
			await page.getByRole('listbox', { name: 'Agents to tag' }).getByRole('button', { name: 'Retry' }).click();
			await expect(page.getByRole('option', { name: /Alpha Agent/ })).toBeVisible();
			await expect(input).toBeFocused();
			expect(await input.evaluate((element: HTMLTextAreaElement) => element.selectionStart)).toBe(caret);
			await input.press('Escape');
			await expect(input).toBeFocused();
			expect(await input.evaluate((element: HTMLTextAreaElement) => element.selectionStart)).toBe(caret);
			await expect(input).toHaveValue('Hello @Al');
			await page.keyboard.type(' xyz', { delay: 5 });
			await expect(input).toHaveValue('Hello @Al xyz');
			await expect(input).toBeFocused();
		});
	}
}
