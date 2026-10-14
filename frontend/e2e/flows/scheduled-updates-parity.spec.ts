import { expect, test, type Page, type TestInfo } from '@playwright/test';

import { TEST_SESSION_TOKEN, openSettings, saveEvidenceScreenshot } from '../support/corsia';

test.use({ video: 'on' });

type Schedule = { automatic: boolean; time: string; tonight_at: string | null };

function updateStatus(schedule: Schedule) {
	return {
		branch: 'dev', current_sha: 'current', remote_sha: 'target', commits_behind: 1,
		available: true, checked_at: Date.now(), error: null,
		current_version: '2026.10.13', target_version: '2026.10.14',
		changes: { headline: 'A calmer update journey', items: ['Schedules stay visible', 'Agents resume safely'] },
		drain_cap_seconds: 60, usual_seconds: 45, agents_working: 2,
		operation: null, schedule,
	};
}

async function openSystem(page: Page, theme: 'dark' | 'light', width: number) {
	await page.setViewportSize({ width, height: width === 393 ? 852 : 1000 });
	await page.goto(`/?token=${encodeURIComponent(TEST_SESSION_TOKEN)}`);
	await expect(page.getByRole('button', { name: 'Toggle sidebar' })).toBeVisible();
	await page.evaluate((nextTheme) => document.documentElement.setAttribute('data-theme', nextTheme), theme);
	await openSettings(page);
	await page.getByRole('button', { name: 'System' }).click();
}

async function captureSettings(page: Page, testInfo: TestInfo, theme: 'dark' | 'light', width: number, schedule: Schedule, name: string) {
	await openSystem(page, theme, width);
	await expect(page.getByText('Keep CORSIA up to date automatically')).toBeVisible();
	const installCopy = page.getByText(`Installs at ${schedule.time === '03:30' ? '3:30' : '3:00'} AM.`);
	if (schedule.automatic) {
		await expect(installCopy).toBeVisible();
		await expect(page.getByLabel('Automatic update time')).toHaveValue(schedule.time);
	} else {
		await expect(installCopy).toHaveCount(0);
	}
	await saveEvidenceScreenshot(page, testInfo, name);
}

test.describe('@surface:settings scheduled updates parity', () => {
	test('captures the approved settings, sheet, retry, and drain-cue matrix', async ({ page }, testInfo) => {
		test.setTimeout(60_000);
		let schedule: Schedule = { automatic: false, time: '03:00', tonight_at: null };
		let rejectScheduleSave = false;
		await page.route('**/api/system/update-status*', (route) => route.fulfill({ json: updateStatus(schedule) }));
		await page.route('**/api/system/update-schedule', async (route) => {
			if (rejectScheduleSave) return route.fulfill({ status: 500, json: { detail: 'save failed' } });
			const patch = route.request().postDataJSON() as Partial<Schedule> & { tonight?: boolean };
			schedule = { ...schedule, ...patch, tonight_at: patch.tonight === true ? '2026-10-14T03:00:00Z' : patch.tonight === false ? null : schedule.tonight_at };
			return route.fulfill({ json: schedule });
		});

		const cells: Array<{ theme: 'dark' | 'light'; width: 1440 | 393; suffix: string }> = [
			{ theme: 'light', width: 1440, suffix: 'desktop-light' },
			{ theme: 'dark', width: 1440, suffix: 'desktop-dark' },
			{ theme: 'light', width: 393, suffix: 'mobile-light' },
			{ theme: 'dark', width: 393, suffix: 'mobile-dark' },
		];

		schedule = { automatic: false, time: '03:00', tonight_at: null };
		for (const cell of cells) await captureSettings(page, testInfo, cell.theme, cell.width, schedule, `settings-off-${cell.suffix}`);

		schedule = { automatic: true, time: '03:30', tonight_at: null };
		for (const cell of cells) await captureSettings(page, testInfo, cell.theme, cell.width, schedule, `settings-on-${cell.suffix}`);

		schedule = { automatic: false, time: '03:00', tonight_at: null };
		for (const cell of cells) {
			await openSystem(page, cell.theme, cell.width);
			await page.getByRole('button', { name: 'Update', exact: true }).click();
			const updateSheet = page.getByLabel('Update CORSIA');
			await expect(updateSheet.getByText('Keep CORSIA up to date automatically')).toBeVisible();
			await expect(updateSheet.getByText('CORSIA will be unavailable for about a minute.')).toBeVisible();
			if (cell.width === 393) {
				await expect(page.locator('[data-adaptive-drawer]')).toBeVisible();
				const geometry = await updateSheet.evaluate((panel) => {
					const drain = panel.querySelector<HTMLElement>('[data-update-drain]')!;
					const actions = panel.querySelector<HTMLElement>('[data-update-mobile-actions]')!;
					const panelBox = panel.getBoundingClientRect();
					const drainBox = drain.getBoundingClientRect();
					const actionsBox = actions.getBoundingClientRect();
					return {
						drainFits: drain.scrollHeight <= drain.clientHeight && drainBox.top >= panelBox.top && drainBox.bottom <= panelBox.bottom - 8,
						actionsReachable: actionsBox.top >= panelBox.top && actionsBox.bottom <= panelBox.bottom,
					};
				});
				expect(geometry).toEqual({ drainFits: true, actionsReachable: true });
			}
			await saveEvidenceScreenshot(page, testInfo, `sheet-drain-cue-${cell.suffix}`);
		}

		schedule = { automatic: true, time: '03:30', tonight_at: null };
		rejectScheduleSave = true;
		for (const cell of cells) {
			await openSystem(page, cell.theme, cell.width);
			await expect(page.getByLabel('Automatic update time')).toHaveValue('03:30');
			await page.getByRole('button', { name: 'Automatically install updates overnight' }).click();
			await expect(page.getByText("Couldn't save the schedule")).toBeVisible();
			await expect(page.getByLabel('Automatic update time')).toHaveValue('03:30');
			await saveEvidenceScreenshot(page, testInfo, `retry-toast-${cell.suffix}`);
		}
	});

	test('records choice-sheet open and close motion for every dismiss path', async ({ page }) => {
		const schedule: Schedule = { automatic: false, time: '03:00', tonight_at: null };
		await page.route('**/api/system/update-status*', (route) => route.fulfill({ json: updateStatus(schedule) }));
		await page.route('**/api/system/update-schedule', (route) => route.fulfill({ json: schedule }));
		await openSystem(page, 'light', 1440);

		const sheet = page.getByLabel('Update CORSIA');
		const open = async () => {
			await page.getByRole('button', { name: 'Update', exact: true }).click();
			await expect(sheet).toBeVisible();
		};
		const closed = async () => expect(sheet).toBeHidden();

		await open();
		await page.getByRole('button', { name: 'Close', exact: true }).click();
		await closed();

		await open();
		await page.getByRole('button', { name: 'Later' }).click();
		await closed();

		await open();
		await page.locator('.bg').click({ position: { x: 8, y: 8 } });
		await closed();

		await open();
		await page.keyboard.press('Escape');
		await closed();
	});
});
