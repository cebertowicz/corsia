import path from 'node:path';
import { test, expect } from '../support/test';
import { openThreadView, postLinks, watchSocket } from '../support/documentOpen';
import { raiseAsk } from '../support/reviewAsk';

test('Activity and chat open the same document with their shared header', async ({ page, request }, info) => {
	test.setTimeout(180_000);
	const phone = info.project.name === 'chromium-mobile';
	const width = phone ? 393 : 1440;
	await page.setViewportSize(phone ? { width, height: 852 } : { width, height: 900 });
	const { threadId, a } = await openThreadView(page, request, watchSocket(page), 'activity-header');
	await postLinks(page, request, `[Open document](<${a.docPath}>)`);
	await page.getByRole('link', { name: 'Open document', exact: true }).click();
	await expect(page.locator('.cm-content')).toContainText(a.body);
	const chatTitleTop = await page.locator('.cm-content .cm-line').first().evaluate((el) => el.getBoundingClientRect().top);
	const suffix = process.env.ACTIVITY_BASELINE ? 'before' : 'after';
	if (process.env.ACTIVITY_OUT) await page.screenshot({ path: path.join(process.env.ACTIVITY_OUT, `chat-document-${width}-${suffix}.png`), animations: 'disabled' });
	raiseAsk(threadId, a.artifactId, 'Review this document');
	if (phone) await page.getByRole('button', { name: 'Chat', exact: true }).click();
	if (phone) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByRole('navigation', { name: 'Global' }).getByRole('button', { name: 'Activity' }).click();
	const row = page.locator('[data-activity-row]').filter({ hasText: 'Review this document' });
	await expect(row).toBeVisible({ timeout: 20_000 });
	if (phone) await row.click();
	await expect(page.locator('.pane-col .cm-content')).toContainText(a.body);
	const activityTitleTop = await page.locator('.pane-col .cm-content .cm-line').first().evaluate((el) => el.getBoundingClientRect().top);
	console.log('TITLE_TOPS', { chatTitleTop, activityTitleTop });
	if (phone) expect(activityTitleTop).toBe(chatTitleTop);
	if (phone && !process.env.ACTIVITY_BASELINE) {
		await expect(page.locator('header').getByRole('button', { name: /Open files/ })).toBeVisible();
		await expect(page.locator('.pane-col [role="tablist"]')).toHaveCount(0);
	}
	if (process.env.ACTIVITY_OUT) await page.screenshot({ path: path.join(process.env.ACTIVITY_OUT, `activity-document-${width}-${suffix}.png`), animations: 'disabled' });
	if (phone && !process.env.ACTIVITY_BASELINE) {
		await page.getByRole('button', { name: /Open files/ }).click();
		await expect(page.locator('.rb-dock')).toBeHidden();
		if (process.env.ACTIVITY_OUT) await page.screenshot({ path: path.join(process.env.ACTIVITY_OUT, 'activity-document-393-tabs-after.png'), animations: 'disabled' });
		await page.getByRole('button', { name: 'Done', exact: true }).click();
		await expect(page.locator('.rb-dock')).toBeVisible();
	}
});
