import path from 'node:path';
import { expect, test, type Page } from '@playwright/test';
import {
	createSession,
	loginByUrl,
	saveEvidenceScreenshot,
	TEST_TOKEN,
} from '../support/corsia';

/**
 * Pinning keeps a document in the thread pane after the task that wrote it
 * settles. This proves the user-facing half at both widths: the pin reads as
 * status, pinned rows lead the pane, and the state survives a reload.
 */
test.describe('@flow:artifact-pin @surface:chat artifact pane pinning', () => {
	test('pins a row to the top, shows it as pinned, and survives reload', async ({ page, request }, testInfo) => {
		test.slow();
		const stamp = Date.now();
		const sessionTitle = `artifact-pin-${stamp}`;
		const sessionId = await createSession(request, sessionTitle);
		const tok = encodeURIComponent(TEST_TOKEN);

		const tree = await (await request.get(`/api/files/tree?token=${tok}`)).json();
		const roots: Array<{ kind: string; path: string }> = tree.roots ?? [];
		const workspaceRoot = (roots.find((root) => root.kind === 'workspace') ?? roots[0])?.path;
		expect(workspaceRoot).toEqual(expect.any(String));

		// Seeded oldest-first, so recency alone would put keeper.md last.
		const keeper = `pin-keeper-${stamp}.md`;
		const churn = `pin-churn-${stamp}.md`;
		for (const name of [keeper, churn]) {
			const seed = await request.put(`/api/files/content?token=${tok}`, {
				data: {
					path: path.join(workspaceRoot, name),
					text: `# ${name}\n\nBody.`,
					project_id: null,
					thread_id: sessionId,
					create: true,
				},
			});
			expect(seed.ok(), `seed failed: ${seed.status()} ${await seed.text()}`).toBeTruthy();
		}

		await loginByUrl(page);
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await page.getByText(sessionTitle).first().click();

		const names = page.locator('.row-outer .name');
		await ensurePaneOpen(page);
		await expect(names).toHaveCount(2);

		// Recency first: the later write leads.
		await expect(names).toHaveText([churn, keeper]);

		const keeperRow = page.locator('.row-outer').filter({ hasText: keeper });
		await keeperRow.click({ button: 'right' });
		await page.getByRole('menuitem', { name: 'Pin to pane' }).click();

		// Pinned rows lead the pane regardless of recency.
		await expect(names).toHaveText([keeper, churn]);
		const pinnedRow = page.locator('.row-outer').filter({ hasText: keeper });
		// One pin mark on the row; Unpin lives in its menu.
		await expect(pinnedRow.locator('.pin-mark')).toHaveCount(1);
		await expect(pinnedRow.locator('.pin-mark')).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'artifact-pane-pinned', { fullPage: false });

		// The pin is server state, not a local flag. Reload lands on the
		// workspace home (single-route app), so re-enter the thread from the
		// sidebar and require the server to return the pinned order.
		await page.reload();
		if ((page.viewportSize()?.width ?? 0) <= 768) {
			await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		}
		await page.getByText(sessionTitle).first().click();
		await ensurePaneOpen(page);
		await expect(names).toHaveText([keeper, churn]);

		// Unpinning drops the priority tier, but the toggle itself updates the
		// registration, so the row just acted on leads the recency tier.
		const stillPinned = page.locator('.row-outer').filter({ hasText: keeper });
		await stillPinned.click({ button: 'right' });
		await page.getByRole('menuitem', { name: 'Unpin from pane' }).click();
		await expect(names).toHaveText([keeper, churn]);
		await expect(page.locator('.row-outer .pin-mark')).toHaveCount(0);
	});
});

/** Open the right pane when it is visually closed. Desktop renders a closed
 *  pane at width 0 with the rows still in the DOM, mobile renders it display:
 *  none — a locator count cannot tell either state from open, so measure. */
async function ensurePaneOpen(page: Page) {
	const width = async () => (await page.locator('aside.pane').boundingBox())?.width ?? 0;
	if ((await width()) < 40) {
		await page.getByRole('button', { name: 'Toggle panel' }).click();
	}
	await expect
		.poll(width, { message: 'right pane did not reach open width' })
		.toBeGreaterThan(200);
}
