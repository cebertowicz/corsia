import { expect, test, type Locator, type Page } from '@playwright/test';
import { appendFileSync, mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { createSession, loginByUrl } from './support/corsia';
import { createDocument } from './support/documentOpen';

const output = process.env.HEADER_OVERLAP_EVIDENCE_DIR;
const phase = process.env.HEADER_OVERLAP_PHASE;
const reuseEvidenceState = process.env.HEADER_OVERLAP_REUSE === '1';
const evidenceThreadTitle = 'header clearance evidence';
const evidenceDocumentName = 'header-clearance-evidence.md';
if (!output || !phase) throw new Error('HEADER_OVERLAP_EVIDENCE_DIR and HEADER_OVERLAP_PHASE are required');

type Condition = { name: string; width: number; height: number; safeTop: number; theme: 'light' | 'dark' };
const conditions: Condition[] = [
	{ name: 'phone-simulated-inset-59-light', width: 393, height: 852, safeTop: 59, theme: 'light' },
	{ name: 'phone-simulated-inset-59-dark', width: 393, height: 852, safeTop: 59, theme: 'dark' },
	{ name: 'phone-inset-0-light', width: 393, height: 852, safeTop: 0, theme: 'light' },
	{ name: 'phone-inset-0-dark', width: 393, height: 852, safeTop: 0, theme: 'dark' },
	{ name: 'desktop-1440-light', width: 1440, height: 1000, safeTop: 0, theme: 'light' },
	{ name: 'desktop-1440-dark', width: 1440, height: 1000, safeTop: 0, theme: 'dark' },
];

async function prepareCondition(page: Page, condition: Condition) {
	await page.setViewportSize({ width: condition.width, height: condition.height });
	await page.emulateMedia({ colorScheme: condition.theme, reducedMotion: 'reduce' });
}

async function applySafeTop(page: Page, condition: Condition) {
	await page.addStyleTag({ content: `:root { --safe-top: ${condition.safeTop}px; }` });
}

async function openActivity(page: Page, condition: Condition) {
	await prepareCondition(page, condition);
	await loginByUrl(page, { waitForComposer: false });
	await applySafeTop(page, condition);
	if (condition.width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.getByRole('button', { name: 'Activity', exact: true }).click();
	await expect(page.getByRole('heading', { name: 'Activity', exact: true })).toBeVisible();
}

async function waitForSettledView(page: Page, view: string, target: Locator) {
	await expect(target).toBeVisible();
	if (view !== 'documents') return;
	await expect(page.getByText(evidenceDocumentName, { exact: true }).first()).toBeVisible();
	await expect(page.getByText("Loading the scope's files…", { exact: true })).toBeHidden();
	await expect(page.getByText('Workspace', { exact: true }).first()).toBeVisible();
}

async function capture(page: Page, condition: Condition, view: string, target: Locator) {
	await waitForSettledView(page, view, target);
	const metrics = await target.evaluate((element) => {
		const content = element.getBoundingClientRect();
		const header = document.querySelector('.shell-header')?.getBoundingClientRect();
		if (!header) throw new Error('shell header is missing');
		return { contentTop: content.top, headerBottom: header.bottom, gap: content.top - header.bottom };
	});
	const file = `${phase}-${condition.name}-${view}.png`;
	const dpr = await page.evaluate(async () => {
		await document.fonts.ready;
		return window.devicePixelRatio;
	});
	await page.screenshot({ path: join(output, file), animations: 'disabled', caret: 'hide', scale: 'css' });
	appendFileSync(join(output, `${phase}-metrics.jsonl`), JSON.stringify({ phase, view, route: new URL(page.url()).pathname, label: condition.safeTop ? 'simulated inset 59 (token override)' : 'inset 0', dpr, ...condition, ...metrics, file }) + '\n');
}

test('captures token-simulated header-clearance evidence', async ({ page, request }) => {
	test.setTimeout(120_000);
	await page.clock.setFixedTime(new Date('2026-01-15T12:00:00.000Z'));
	mkdirSync(output, { recursive: true });
	if (!reuseEvidenceState) {
		const threadId = await createSession(request, evidenceThreadTitle);
		await createDocument(request, threadId, evidenceDocumentName);
	}
	for (const condition of conditions) {
		await openActivity(page, condition);
		await capture(page, condition, 'activity-at-rest', page.getByRole('heading', { name: 'Activity', exact: true }));

		await prepareCondition(page, condition);
		await loginByUrl(page, { waitForComposer: false });
		await applySafeTop(page, condition);
		if (condition.width <= 768) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
		await page.getByText(evidenceThreadTitle, { exact: true }).first().click();
		const browseDocuments = page.getByRole('button', { name: 'Browse documents…' });
		if (!(await browseDocuments.isVisible())) await page.getByRole('button', { name: 'Toggle panel' }).click();
		await browseDocuments.click();
		await page.getByPlaceholder('Search docs…').fill(evidenceDocumentName);
		await capture(page, condition, 'documents', page.getByPlaceholder('Search docs…'));

		await prepareCondition(page, condition);
		await loginByUrl(page);
		await applySafeTop(page, condition);
		await capture(page, condition, 'chat-at-top', page.getByRole('textbox', { name: 'Message CORSIA' }));
	}
});
