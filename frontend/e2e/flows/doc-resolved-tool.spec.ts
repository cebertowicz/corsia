import { expect } from '@playwright/test';
import { test } from '../support/test';
import { closeResolved, resolvedEntry, resolvedList } from '../support/commentDrawer';
import { resolvedTool } from '../support/documentBar';
import { openLoaded, seedLoaded } from '../support/loadedComments';
import { TEST_TOKEN } from '../support/corsia';

// The document row's Resolved · N (above the phone width, as before 0.5.0): one press opens the comments on their Resolved view,
// from closed or from the Open view, and a second press leaves it. On a phone the drawer's own switch is the way in, so there is no
// such control in the row.

test("the row's Resolved · N opens the comments on their Resolved view from closed and from the Open view (1440)", async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop', 'The row control is drawn above the phone width');
	test.setTimeout(240_000);
	const doc = await seedLoaded(request, 'resolved tool');
	for (const id of doc.threads.slice(0, 3)) {
		const patched = await request.patch(`/api/comments/${id}`, { params: { token: TEST_TOKEN }, data: { resolved: true } });
		expect(patched.ok(), await patched.text()).toBeTruthy();
	}
	await openLoaded(page, doc, { width: 1440, height: 900, theme: 'light', touch: false, pane: 'open' });
	const drawer = page.getByRole('region', { name: 'Document comments', exact: true });
	const tool = resolvedTool(page);
	await expect(tool, 'the row names how many are resolved').toHaveText('Resolved · 3');
	await expect(tool).toHaveAttribute('aria-expanded', 'false');

	// Closed: one press opens the comments already on Resolved.
	await expect(drawer).toHaveCount(0);
	await tool.click();
	await expect(drawer).toBeVisible();
	await expect(resolvedList(page)).toHaveCount(1);
	await expect(resolvedEntry(drawer)).toHaveAttribute('aria-pressed', 'true');
	await expect(tool).toHaveAttribute('aria-expanded', 'true');

	// Pressed again from the Resolved view: it leaves.
	await tool.click();
	await expect(drawer).toHaveCount(0);
	await expect(tool).toHaveAttribute('aria-expanded', 'false');

	// Open view: the comments open on Open, and the row's control switches them to Resolved without closing.
	await page.locator('[data-comments-entry]').click();
	await expect(drawer).toBeVisible();
	await expect(resolvedList(page)).toHaveCount(0);
	await expect(tool).toHaveAttribute('aria-expanded', 'false');
	await tool.click();
	await expect(drawer).toBeVisible();
	await expect(resolvedList(page)).toHaveCount(1);
	await expect(tool).toHaveAttribute('aria-expanded', 'true');

	// The drawer's own switch still goes back to Open.
	await closeResolved(drawer);
	await expect(tool).toHaveAttribute('aria-expanded', 'false');
});
