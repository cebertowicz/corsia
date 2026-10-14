import { expect, test, type Page, type TestInfo } from '@playwright/test';

import { TEST_SESSION_TOKEN, openSettings, saveEvidenceScreenshot } from '../support/corsia';

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
	await expect(page.getByText(`Installs at ${schedule.time === '03:30' ? '3:30' : '3:00'} AM.`)).toBeVisible();
	if (schedule.automatic) await expect(page.getByLabel('Automatic update time')).toHaveValue(schedule.time);
	await saveEvidenceScreenshot(page, testInfo, name);
}

test.describe('@surface:settings scheduled updates parity', () => {
	test('captures the approved settings, sheet, retry, and drain-cue matrix', async ({ page }, testInfo) => {
		let schedule: Schedule = { automatic: false, time: '03:00', tonight_at: null };
		let rejectScheduleSave = false;
		await page.route('**/api/system/update-status*', (route) => route.fulfill({ json: updateStatus(schedule) }));
		await page.route('**/api/system/update-schedule', async (route) => {
			if (rejectScheduleSave) return route.fulfill({ status: 500, json: { detail: 'save failed' } });
			const patch = route.request().postDataJSON() as Partial<Schedule> & { tonight?: boolean };
			schedule = { ...schedule, ...patch, tonight_at: patch.tonight === true ? '2026-10-14T03:00:00Z' : patch.tonight === false ? null : schedule.tonight_at };
			return route.fulfill({ json: schedule });
		});

		schedule = { automatic: false, time: '03:00', tonight_at: null };
		await captureSettings(page, testInfo, 'dark', 1440, schedule, 'settings-off-desktop-dark');
		await captureSettings(page, testInfo, 'light', 393, schedule, 'settings-off-mobile-light');

		schedule = { automatic: true, time: '03:30', tonight_at: null };
		await captureSettings(page, testInfo, 'light', 1440, schedule, 'settings-on-desktop-light');
		await captureSettings(page, testInfo, 'dark', 393, schedule, 'settings-on-mobile-dark');

		schedule = { automatic: false, time: '03:00', tonight_at: null };
		await openSystem(page, 'dark', 1440);
		await page.getByRole('button', { name: 'Update', exact: true }).click();
		const updateSheet = page.getByLabel('Update CORSIA');
		await expect(updateSheet.getByText('Keep CORSIA up to date automatically')).toBeVisible();
		await expect(updateSheet.getByText('CORSIA will be unavailable for about a minute.')).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'sheet-drain-cue-desktop-dark');

		await openSystem(page, 'light', 393);
		await page.getByRole('button', { name: 'Update', exact: true }).click();
		const mobileUpdateSheet = page.getByLabel('Update CORSIA');
		await expect(mobileUpdateSheet.getByText('Keep CORSIA up to date automatically')).toBeVisible();
		await expect(mobileUpdateSheet.getByText('CORSIA will be unavailable for about a minute.')).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'sheet-drain-cue-mobile-light');

		rejectScheduleSave = true;
		await openSystem(page, 'light', 1440);
		await page.getByRole('button', { name: 'Automatically install updates overnight' }).click();
		await expect(page.getByText("Couldn't save the schedule")).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'retry-toast-desktop-light');

		await openSystem(page, 'dark', 393);
		await page.getByRole('button', { name: 'Automatically install updates overnight' }).click();
		await expect(page.getByText("Couldn't save the schedule")).toBeVisible();
		await saveEvidenceScreenshot(page, testInfo, 'retry-toast-mobile-dark');
	});
});
