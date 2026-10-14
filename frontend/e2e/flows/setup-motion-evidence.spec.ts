import { expect, test, type Locator, type Page } from '@playwright/test';

test.use({ video: { mode: 'on', size: { width: 1280, height: 900 } } });

async function openFixture(page: Page, reducedMotion: 'reduce' | 'no-preference' = 'no-preference') {
	await page.setViewportSize({ width: 1280, height: 900 });
	await page.emulateMedia({ colorScheme: 'light', reducedMotion });
	await page.goto('/_design/setup-motion');
	await expect(page.getByRole('heading', { name: 'Setup motion fixture' })).toBeVisible();
}

async function waitForSettled(page: Page, target: Locator) {
	const duration = await target.evaluate((element) => {
		const token = getComputedStyle(element).getPropertyValue('--dur-standard').trim();
		const value = Number.parseFloat(token) || 0;
		return value * (token.endsWith('ms') ? 2 : 2_000);
	});
	await page.waitForTimeout(duration);
}

async function assertDoneCheckSettled(target: Locator) {
	await expect(target.locator('.done-check path').evaluate((path) => Number.parseFloat(getComputedStyle(path).strokeDashoffset))).resolves.toBe(0);
}

async function hold(page: Page, target: Locator) {
	await target.hover();
	await page.mouse.down();
}

const css = (target: Locator, property: string) => target.evaluate((element, name) => getComputedStyle(element).getPropertyValue(name), property);

test.describe('@surface:setup-rail motion evidence', () => {
	test('records setup rail state, job-row, and press motion', async ({ page }, testInfo) => {
		test.setTimeout(60_000);
		await openFixture(page);

		const step = page.locator('.rail .row').filter({ hasText: 'Motion step' });
		const advanceStep = page.getByRole('button', { name: 'Advance step', exact: true });
		await expect(step).toHaveClass(/todo/);
		await advanceStep.click();
		await expect(step).toHaveClass(/current/);
		await advanceStep.click();
		await expect(step).toHaveClass(/done/);
		await waitForSettled(page, step);
		await expect(step.locator('.done-check')).toBeVisible();
		await assertDoneCheckSettled(step);
		await page.screenshot({ path: testInfo.outputPath('setup-motion-rail-todo-current-done-light-1280x900.png'), scale: 'css' });

		const advanceState = page.getByRole('button', { name: 'Advance background state', exact: true });
		await page.getByRole('button', { name: 'Advance background jobs', exact: true }).click();
		const firstJob = page.locator('.job-row').filter({ hasText: 'First background job' });
		const firstJobRail = firstJob.locator('.row');
		await waitForSettled(page, firstJob);
		await expect(firstJob).toHaveCSS('opacity', '1');
		await expect.poll(() => firstJob.evaluate((element) => element.getBoundingClientRect().height)).toBeGreaterThan(0);
		await page.screenshot({ path: testInfo.outputPath('setup-motion-jobs-one-light-1280x900.png'), scale: 'css' });
		await page.getByRole('button', { name: 'Advance background jobs', exact: true }).click();
		await expect(page.locator('.job-row')).toHaveCount(2);
		await page.screenshot({ path: testInfo.outputPath('setup-motion-jobs-two-light-1280x900.png'), scale: 'css' });
		await page.getByRole('button', { name: 'Advance background jobs', exact: true }).click();
		await expect(page.locator('.job-row')).toHaveCount(0);
		await page.screenshot({ path: testInfo.outputPath('setup-motion-jobs-leaving-light-1280x900.png'), scale: 'css' });

		await page.getByRole('button', { name: 'Advance background jobs', exact: true }).click();
		await expect(firstJobRail).toHaveClass(/busy/);
		await advanceState.click();
		await expect(firstJobRail).toHaveClass(/done/);
		await advanceState.click();
		await expect(firstJobRail).toHaveClass(/error/);
		await expect(firstJob.getByText('Couldn’t finish', { exact: true })).toBeVisible();
		await page.screenshot({ path: testInfo.outputPath('setup-motion-job-busy-done-error-light-1280x900.png'), scale: 'css' });

		for (const [name, target] of [
			['pill', page.getByRole('button', { name: 'Advance step', exact: true })],
			['chip', page.getByRole('button', { name: 'Advance background state', exact: true })],
			['rail-row', page.getByRole('button', { name: 'Pressable rail row', exact: true })],
		] as const) {
			await hold(page, target);
			if (name !== 'rail-row') await expect.poll(() => css(target, 'transform')).not.toBe('none');
			await page.screenshot({ path: testInfo.outputPath(`setup-motion-${name}-pressed-light-1280x900.png`), scale: 'css' });
			await page.mouse.up();
		}
	});

	test('records the reduced-motion setup rail pass', async ({ page }, testInfo) => {
		await openFixture(page, 'reduce');
		const advanceState = page.getByRole('button', { name: 'Advance background state', exact: true });
		const advanceJobs = page.getByRole('button', { name: 'Advance background jobs', exact: true });
		await advanceJobs.click();
		const job = page.locator('.job-row').filter({ hasText: 'First background job' });
		await expect(job.locator('.spinner')).toBeVisible();
		await expect(job.locator('.spinner').evaluate((element) => getComputedStyle(element).animationName)).resolves.toBe('none');
		await hold(page, advanceState);
		await expect(css(advanceState, 'transform')).resolves.toBe('none');
		await page.screenshot({ path: testInfo.outputPath('setup-motion-reduced-motion-light-1280x900.png'), scale: 'css' });
		await page.mouse.up();
	});
});
