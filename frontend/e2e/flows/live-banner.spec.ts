import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test, type Page, type TestInfo } from '@playwright/test';
import { createSession, openAppByUrl } from '../support/corsia';

const LONG_BASH_PHRASE = "Running grep -rn --hidden --glob='*.ts' 'live work banner truncation must preserve the full command in details' frontend/src backend/tests";
const LIVE_LABEL = `Working 5 min · ${LONG_BASH_PHRASE}`;

async function openThread(page: Page, title: string): Promise<void> {
	await openAppByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) {
		await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	}
	await page.getByText(title, { exact: true }).first().click();
}

async function showLiveBanner(page: Page, threadId: string): Promise<void> {
	await page.evaluate(async ({ id, phrase }) => {
		const [settings, threads, turns] = await Promise.all([
			import('/src/lib/stores/settings.ts'),
			import('/src/lib/stores/threads.ts'),
			import('/src/lib/ws/turnState.ts'),
		]);
		settings.coreSettings.set({ features: { focus_mode: true } } as any);
		threads.activeToolsByThread.set(new Map([[id, [{
			id: 'long-bash', name: 'bash', phrase, status: 'running',
		}]]]) as any);
		turns.applyLifecycle(id, { turnId: 'live-banner-turn', turnOrder: 1 }, undefined, Date.now() - 5 * 60_000);
	}, { id: threadId, phrase: LONG_BASH_PHRASE });
}

async function saveProof(page: Page, testInfo: TestInfo, name: string): Promise<void> {
	const root = process.env.LIVE_BANNER_PROOF_DIR ?? testInfo.outputPath('live-banner-proof');
	mkdirSync(root, { recursive: true });
	await page.screenshot({ path: path.join(root, `${name}.png`), fullPage: false, animations: 'disabled' });
}

test('@surface:live-banner Focus shows a time-first single-line live bash step on desktop and mobile', async ({ page, request }, testInfo) => {
	test.setTimeout(90_000);
	const title = `live-banner-proof-${Date.now()}`;
	const threadId = await createSession(request, title);

	await page.setViewportSize({ width: 1440, height: 900 });
	await openThread(page, title);
	await showLiveBanner(page, threadId);

	const banner = page.locator('.live-work .live-step');
	const toggle = page.locator('.live-work .fold-toggle');
	await expect(banner).toHaveText(LIVE_LABEL);
	await expect(banner).toHaveAttribute('title', LIVE_LABEL);
	await expect(toggle).toHaveAttribute('aria-label', `Expand current work: ${LIVE_LABEL}`);
	await expect(page.locator('.live-work .live-spinner')).toBeVisible();
	await expect(page.locator('.live-work .fold-chevron')).toBeVisible();
	expect(await banner.evaluate((element) => {
		const label = getComputedStyle(element);
		const control = getComputedStyle(element.closest('.fold-toggle')!);
		return { label: [label.whiteSpace, label.overflow, label.textOverflow, label.minWidth], controlMinWidth: control.minWidth };
	})).toEqual({ label: ['nowrap', 'hidden', 'ellipsis', '0px'], controlMinWidth: '0px' });
	await saveProof(page, testInfo, 'live-banner-1440x900');

	await page.setViewportSize({ width: 393, height: 852 });
	await expect(banner).toHaveText(LIVE_LABEL);
	await expect(page.locator('.live-work .live-spinner')).toBeVisible();
	await expect(page.locator('.live-work .fold-chevron')).toBeVisible();
	await saveProof(page, testInfo, 'live-banner-393x852');
});
