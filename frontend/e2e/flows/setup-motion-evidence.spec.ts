import { expect, test, type Locator, type Page } from '@playwright/test';

test.use({ video: { mode: 'on', size: { width: 1280, height: 900 } } });

async function openFixture(page: Page, reducedMotion: 'reduce' | 'no-preference' = 'no-preference') {
	await page.setViewportSize({ width: 1280, height: 900 });
	await page.addInitScript(() => localStorage.setItem('corsia_theme', 'auto'));
	await page.emulateMedia({ colorScheme: 'light', reducedMotion });
	await page.goto('/_design/setup-motion');
	await page.evaluate(() => {
		localStorage.setItem('corsia_theme', 'light');
		document.documentElement.setAttribute('data-theme', 'light');
	});
	await expect(page.getByRole('heading', { name: 'Setup motion fixture' })).toBeVisible();
	await expect(page.locator('html')).toHaveAttribute('data-theme', 'light');
	await expect(page.locator('.setup-shell')).toHaveCSS('background-color', 'rgb(255, 255, 255)');
}

async function tokenDuration(target: Locator, tokenName: '--dur-standard' | '--dur-tight') {
	return target.evaluate((element, name) => {
		const token = getComputedStyle(element).getPropertyValue(name).trim();
		const value = Number.parseFloat(token) || 0;
		return value * (token.endsWith('ms') ? 1 : 1_000);
	}, tokenName);
}

async function waitForSettled(page: Page, target: Locator) {
	await page.waitForTimeout((await tokenDuration(target, '--dur-standard')) * 2);
}

async function assertDoneCheckSettled(target: Locator) {
	await expect(target.locator('.done-check path').evaluate((path) => Number.parseFloat(getComputedStyle(path).strokeDashoffset))).resolves.toBe(0);
}

async function hold(page: Page, target: Locator) {
	await target.hover();
	await page.mouse.down();
}

const css = (target: Locator, property: string) => target.evaluate((element, name) => getComputedStyle(element).getPropertyValue(name), property);

async function toggleJobsAndMeasureHalfway(page: Page) {
	return page.evaluate(async () => {
		const button = Array.from(document.querySelectorAll('button')).find((element) => element.textContent?.trim() === 'Advance background jobs');
		if (!(button instanceof HTMLButtonElement)) throw new Error('Missing background-job control');
		button.click();
		await new Promise<void>((resolve) => requestAnimationFrame(() => resolve()));
		const region = document.querySelector<HTMLElement>('.job-region');
		if (!region) throw new Error('Missing background-job region');
		const token = getComputedStyle(region).getPropertyValue('--dur-standard').trim();
		const duration = (Number.parseFloat(token) || 0) * (token.endsWith('ms') ? 1 : 1_000);
		await new Promise<void>((resolve) => window.setTimeout(resolve, duration / 2));
		return region.getBoundingClientRect().height;
	});
}

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
		const advanceJobs = page.getByRole('button', { name: 'Advance background jobs', exact: true });
		const jobRegion = page.locator('.job-region');
		const firstJob = page.locator('.job-row').filter({ hasText: 'First background job' });
		const firstJobRail = firstJob.locator('.row');
		const openingHeight = await toggleJobsAndMeasureHalfway(page);
		await expect(jobRegion).toBeVisible();
		await waitForSettled(page, jobRegion);
		const openHeight = await jobRegion.evaluate((element) => element.getBoundingClientRect().height);
		expect(openingHeight).toBeGreaterThan(0);
		expect(openingHeight).toBeLessThan(openHeight);
		await expect(firstJob).toHaveCSS('opacity', '1');
		await expect.poll(() => firstJob.evaluate((element) => element.getBoundingClientRect().height)).toBeGreaterThan(0);

		await advanceJobs.click();
		await expect(page.locator('.job-row')).toHaveCount(2);
		await waitForSettled(page, page.locator('.job-row').nth(1));
		await page.screenshot({ path: testInfo.outputPath('setup-motion-jobs-two-settled-light-1280x900.png'), scale: 'css' });
		await advanceJobs.click();
		await expect(page.locator('.job-row')).toHaveCount(1);
		await waitForSettled(page, firstJob);
		await page.screenshot({ path: testInfo.outputPath('setup-motion-jobs-one-settled-light-1280x900.png'), scale: 'css' });
		const closingFullHeight = await jobRegion.evaluate((element) => element.getBoundingClientRect().height);
		const closingHeight = await toggleJobsAndMeasureHalfway(page);
		expect(closingHeight).toBeGreaterThan(0);
		expect(closingHeight).toBeLessThan(closingFullHeight);
		await expect(jobRegion).toHaveCount(0);
		await page.screenshot({ path: testInfo.outputPath('setup-motion-jobs-none-settled-light-1280x900.png'), scale: 'css' });

		await advanceJobs.click();
		await waitForSettled(page, firstJob);
		await expect(firstJobRail).toHaveClass(/busy/);
		await advanceState.click();
		await expect(firstJobRail).toHaveClass(/done/);
		await waitForSettled(page, firstJobRail);
		await assertDoneCheckSettled(firstJobRail);
		await page.screenshot({ path: testInfo.outputPath('setup-motion-job-done-settled-light-1280x900.png'), scale: 'css' });
		await advanceState.click();
		await expect(firstJobRail).toHaveClass(/error/);
		await expect(firstJob.getByText('Couldn’t finish', { exact: true })).toBeVisible();
		await page.waitForTimeout((await tokenDuration(firstJobRail, '--dur-tight')) / 2);
		await expect(firstJobRail.locator('.done-check')).toHaveCount(0);
		await page.screenshot({ path: testInfo.outputPath('setup-motion-job-error-settled-light-1280x900.png'), scale: 'css' });

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
