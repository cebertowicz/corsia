import { expect, test, type Page, type TestInfo } from '@playwright/test';

import { chatComposer, createSession, openAppPastLanding, openSettings, saveEvidenceScreenshot, sidebarThread } from '../support/corsia';

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
	await openAppPastLanding(page);
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

		// The decision lives in the Updates card: changes, one cost sentence, Update now and Tonight, and no sheet opens.
		schedule = { automatic: false, time: '03:00', tonight_at: null };
		for (const cell of cells) {
			await openSystem(page, cell.theme, cell.width);
			const card = page.locator('[data-settings-anchor="update"]');
			await expect(card.getByText('Schedules stay visible')).toBeVisible();
			await expect(card.getByText('CORSIA restarts for about a minute; 2 working agents pause and pick up where they left off.')).toBeVisible();
			await expect(card.getByRole('button', { name: 'Update now' })).toBeVisible();
			await expect(card.getByRole('button', { name: 'Tonight at 3:00 AM' })).toBeVisible();
			await expect(card.getByText('Keep CORSIA up to date automatically')).toBeVisible();
			await expect(page.getByRole('dialog')).toHaveCount(0);
			await saveEvidenceScreenshot(page, testInfo, `offer-card-${cell.suffix}`);
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

	for (const viewport of [
		{ width: 1440 as const, label: '1440 desktop' },
		{ width: 393 as const, label: '393 mobile' },
	]) {
		test(`${viewport.label}: the update notice opens Settings at the Updates card and Back returns to the chat`, async ({ page, request }) => {
			test.skip(test.info().project.name.includes('mobile') !== (viewport.width === 393), 'each run uses the project matching its asserted viewport');
			const schedule: Schedule = { automatic: false, time: '03:00', tonight_at: null };
			await page.route('**/api/system/update-status*', (route) => route.fulfill({ json: updateStatus(schedule) }));
			await page.setViewportSize({ width: viewport.width, height: viewport.width === 393 ? 852 : 1000 });
			// A saved chat gives Back something to return to: "New chat" opens an unsaved draft that Back does not return to.
			const title = `e2e scheduled updates back ${Date.now()}`;
			await createSession(request, title);
			await openAppPastLanding(page);
			if (viewport.width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await sidebarThread(page, title).click();
			await expect(chatComposer(page)).toBeVisible();

			const notice = page.getByRole('button', { name: 'Update ready. Open Updates' });
			await expect(notice).toBeVisible();
			await notice.click();
			await expect(page.locator('[data-settings-anchor="update"]').getByRole('button', { name: 'Update now' })).toBeInViewport();
			// Back returns to the chat the notice was opened from, not to Activity. The header back names where it goes: on a
			// phone the deep-linked section first goes up to the Settings list, and the next back leaves Settings.
			if (viewport.width === 393) await page.getByRole('button', { name: 'Settings', exact: true }).click();
			await page.locator('.shell-header .h-back, [aria-label="Close settings"]').first().click();
			await expect(chatComposer(page)).toBeVisible();
		});
	}
});
