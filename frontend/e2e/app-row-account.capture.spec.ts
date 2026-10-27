import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { expect, type Page } from '@playwright/test';
import { test } from './support/test';
import { loginByUrl, openSettings, openSettingsSection } from './support/corsia';

const outputDir = process.env.CORSIA_CAPTURE_DIR ?? 'test-results/app-row-account';
const all = { mode: 'all' as const, project_ids: [], outside_projects: false };
const ACCOUNT = 'firstname.lastname@longcompanydomain.com';
const place = { mode: 'selected' as const, project_ids: [], outside_projects: false };
const baseServers = [
	{ id: 'figma', name: 'Figma', state: 'working', revision: 3, tool_count: 12, reach: all, transport: 'http', sign_in: { status: 'idle', signed_in: true, account: ACCOUNT } },
	{ id: 'notes', name: 'Notes', state: 'working', revision: 1, tool_count: 2, reach: all },
	{ id: 'github', name: 'GitHub', state: 'awaiting_approval', revision: 1, reach: all },
];
// The same app asking for a place first: its one primary action is Choose where it works….
const needsPlace = baseServers.map((server) => server.id === 'github' ? { ...server, reach: place } : server);

async function shot(page: Page, name: string) {
	await page.waitForTimeout(400);
	mkdirSync(outputDir, { recursive: true });
	await page.screenshot({ path: join(outputDir, `${name}.png`), fullPage: false, animations: 'disabled' });
}

test.describe('App row account capture', () => {
	test.skip(process.env.CORSIA_CAPTURE_APP_ROWS !== '1', 'Capture-only proof; enable explicitly when producing the evidence pack.');
	test.setTimeout(120_000);

	for (const theme of ['light', 'dark'] as const) {
		test(`captures app rows in ${theme}`, async ({ page }, testInfo) => {
			const mobile = testInfo.project.name === 'chromium-mobile';
			test.skip(!mobile && theme === 'dark', 'Desktop needs 1440 light only.');
			let servers: Record<string, unknown>[] = baseServers;
			await page.route('**/api/mcp/servers**', async (route) => {
				if (route.request().method() !== 'GET') return route.continue();
				await route.fulfill({ json: { accounts: [], servers, sign_in_here: false } });
			});
			await page.route('**/api/projects**', (route) => route.fulfill({ json: { projects: [] } }));
			await page.emulateMedia({ colorScheme: theme });
			await loginByUrl(page, { waitForComposer: false });
			await page.setViewportSize(mobile ? { width: 393, height: 852 } : { width: 1440, height: 1000 });
			const width = mobile ? '393' : '1440';
			await openSettings(page);
			await openSettingsSection(page, 'Connected apps');
			const title = page.getByRole('button', { name: `Figma — ${ACCOUNT}`, exact: true });
			await expect(title).toBeVisible();
			await expect(page.locator('[data-connected-app-id="figma"] .sub')).toHaveText('Connected · 12 actions · All projects');
			// The app name never truncates; the account gives way first, and the meta stays one line.
			const fit = await page.locator('[data-connected-app-id="figma"]').evaluate((row) => {
				const name = row.querySelector<HTMLElement>('.app-name')!;
				const account = row.querySelector<HTMLElement>('.app-account')!;
				const sub = row.querySelector<HTMLElement>('.sub')!;
				return { nameCut: name.scrollWidth > name.clientWidth + 0.5, accountCut: account.scrollWidth > account.clientWidth + 0.5, subLines: Math.round(sub.getBoundingClientRect().height / parseFloat(getComputedStyle(sub).lineHeight)) };
			});
			expect(fit.nameCut).toBe(false);
			expect(fit.subLines).toBe(1);
			if (mobile) expect(fit.accountCut).toBe(true);
			await shot(page, `${mobile ? '01-collapsed' : '05-collapsed'}-${width}-${theme}`);
			if (!mobile) return;
			await title.click();
			await expect(page.getByRole('button', { name: 'Where it works…' })).toBeVisible();
			await expect(page.getByText('Signed in as')).toHaveCount(0);
			await page.mouse.move(0, 0);
			await shot(page, `02-open-${width}-${theme}`);
			// A chat link lands focus on the row's one primary action; the shot is taken only once that exact control holds focus.
			const linkTo = async (id: string, action: string) => {
				await page.reload();
				await openSettings(page);
				await openSettingsSection(page, 'Connected apps');
				await expect(title).toBeVisible();
				await page.evaluate((hash) => { window.location.hash = hash; }, `connected-apps/${id}`);
				const target = page.locator(`[data-connected-app-id="${id}"] [data-chat-link-focus] .btn`);
				await expect(target).toBeFocused();
				await expect(page.locator(':focus')).toHaveAccessibleName(action);
				await expect(target).toHaveCSS('outline-style', 'solid');
			};
			await linkTo('github', 'Approve');
			await shot(page, `03-chat-link-focus-approve-${width}-${theme}`);
			await page.evaluate(() => { history.replaceState(null, '', window.location.pathname); });
			servers = needsPlace;
			await linkTo('github', 'Choose where it works…');
			await shot(page, `04-chat-link-focus-place-${width}-${theme}`);
			await page.evaluate(() => { history.replaceState(null, '', window.location.pathname); });
			servers = [{ id: 'linear', name: 'Linear', state: 'working', revision: 1, tool_count: 5, reach: all, sign_in: { status: 'idle', signed_in: true } }];
			await page.reload();
			await openSettings(page);
			await openSettingsSection(page, 'Connected apps');
			const linear = page.locator('[data-connected-app-id="linear"]');
			const meta = linear.locator('.sub');
			await expect(meta).toHaveText('Connected · Signed in · 5 actions · All projects');
			expect(await meta.evaluate((node) => Math.round(node.getBoundingClientRect().height / parseFloat(getComputedStyle(node).lineHeight)))).toBe(1);
			await shot(page, `06-no-account-collapsed-${width}-${theme}`);
			await linear.getByRole('button', { name: 'Linear', exact: true }).click();
			await expect(linear.getByText("Linear didn't say which account")).toBeVisible();
			await page.mouse.move(0, 0);
			await shot(page, `07-no-account-open-${width}-${theme}`);
		});
	}
});
