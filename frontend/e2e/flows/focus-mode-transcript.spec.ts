import { execFileSync } from 'node:child_process';
import { mkdirSync } from 'node:fs';
import path from 'node:path';
import { expect, test } from '@playwright/test';
import { createSession, openAppByUrl, openSettings, saveEvidenceScreenshot, TEST_TOKEN } from '../support/corsia';
import { injectMessage } from '../support/mock';
import { setFocusMode } from '../support/focusMode';

// Specs here turn Focus off through Settings; Focus is a persisted server setting, so restore its default.
test.afterEach(async ({ request }) => {
	await setFocusMode(request, true);
});

function seedTerminalTurns(
	humanMessageId: string,
	threadId: string,
	replyTurn: string,
	awayTurn: string,
) {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	const python = process.env.CORSIA_E2E_BACKEND_PYTHON;
	if (!stateRoot || !python || path.basename(stateRoot) !== 'e2e-state') {
		throw new Error('Focus proof requires the disposable E2E Python and state root');
	}
	execFileSync(python, ['-c', [
		'import sqlite3, sys',
		'connection = sqlite3.connect(sys.argv[1])',
		'row = connection.execute("SELECT role FROM messages WHERE id = ?", (sys.argv[2],)).fetchone()',
		'assert row is not None and row[0] == "user"',
		'connection.executemany(',
		'  "INSERT INTO active_turns (id, thread_id, execution_epoch, model, status, started_at, ended_at, boot_epoch) VALUES (?, ?, 0, \'e2e/mock\', \'completed\', \'2026-09-30T12:00:00Z\', \'2026-09-30T12:05:00Z\', \'e2e\')",',
		'  [(sys.argv[3], sys.argv[5]), (sys.argv[4], sys.argv[5])],',
		')',
		'connection.execute("UPDATE messages SET active_turn_id = ? WHERE id = ?", (sys.argv[3], sys.argv[2]))',
		'connection.commit()',
		'connection.close()',
	].join('\n'), path.join(stateRoot, 'corsia.db'), humanMessageId, replyTurn, awayTurn, threadId]);
}

async function closeSettings(page: import('@playwright/test').Page) {
	// Phone: the header's one back names the page it returns to ("Settings", SettingsView.svelte), not "Back".
	const button = (page.viewportSize()?.width ?? 0) <= 768
		? page.locator('.shell-header .h-back')
		: page.getByRole('button', { name: 'Close settings' });
	await button.click();
}

async function openThread(page: import('@playwright/test').Page, title: string) {
	await openAppByUrl(page);
	if ((page.viewportSize()?.width ?? 0) <= 768) {
		await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	}
	await page.getByText(title, { exact: true }).first().click();
}

async function openProofThread(page: import('@playwright/test').Page, title: string) {
	await openThread(page, title);
	await expect(page.getByText('Important update for you.')).toBeVisible();
}

async function ensureFocusMode(page: import('@playwright/test').Page) {
	await openSettings(page);
	await page.getByRole('button', { name: 'System', exact: true }).click();
	const focusToggle = page.getByRole('button', { name: 'Focus mode', exact: true });
	if (await focusToggle.getAttribute('aria-pressed') === 'false') await focusToggle.click();
	await expect(focusToggle).toHaveAttribute('aria-pressed', 'true');
	await closeSettings(page);
}

async function saveFocusPagingProof(page: import('@playwright/test').Page, testInfo: import('@playwright/test').TestInfo, name: string) {
	const root = process.env.FOCUS_PAGING_PROOF_DIR ?? testInfo.outputPath('focus-paging-proof');
	mkdirSync(root, { recursive: true });
	await page.screenshot({ path: path.join(root, `${name}.png`), fullPage: false, animations: 'disabled' });
}

test('@surface:model-attribution a user-directed update renders its recorded exact model', async ({ page, request }, testInfo) => {
	const title = `model-attribution-${Date.now()}`;
	const threadId = await createSession(request, title);
	await injectMessage(request, threadId, {
		role: 'assistant', content: 'Your requested update is ready.',
		metadata: { for_user: true, model: 'codex/gpt-5-codex' },
	}, TEST_TOKEN);

	await page.setViewportSize({ width: 1280, height: 852 });
	await openThread(page, title);
	await expect(page.getByText('Your requested update is ready.')).toBeVisible();
	await expect(page.locator('.msg-model-badge').filter({ hasText: 'Codex · GPT-5 Codex' })).toBeVisible();
	await saveEvidenceScreenshot(page, testInfo, 'user-directed-update-codex-model');
});

test('@surface:focus-paging Focus pages history through the first visible entry', async ({ page, request }, testInfo) => {
	test.setTimeout(90_000);
	const title = `focus-paging-proof-${Date.now()}`;
	const threadId = await createSession(request, title);
	await injectMessage(request, threadId, {
		role: 'user', content: 'Please show the two updates.', metadata: {},
	}, TEST_TOKEN);
	await injectMessage(request, threadId, {
		role: 'assistant', content: 'Older visible reply.', metadata: { for_user: true },
	}, TEST_TOKEN);
	for (let index = 0; index < 121; index++) {
		await injectMessage(request, threadId, {
			role: 'assistant', content: `Older tool-only step ${index + 1}.`,
			metadata: { tool_calls: [{ id: `older-tool-${index}`, name: 'read_file', arguments: {} }] },
		}, TEST_TOKEN);
	}
	for (let index = 0; index < 30; index++) {
		await injectMessage(request, threadId, {
			role: 'assistant', content: `Preload visible reply ${index + 1}.`, metadata: { for_user: true },
		}, TEST_TOKEN);
	}
	await injectMessage(request, threadId, {
		role: 'assistant', content: 'Current visible reply.', metadata: { for_user: true },
	}, TEST_TOKEN);
	for (let index = 0; index < 121; index++) {
		await injectMessage(request, threadId, {
			role: 'assistant', content: `Newer tool-only step ${index + 1}.`,
			metadata: { tool_calls: [{ id: `newer-tool-${index}`, name: 'read_file', arguments: {} }] },
		}, TEST_TOKEN);
	}

	await page.setViewportSize({ width: 1440, height: 900 });
	await openThread(page, title);
	await expect(page.getByText('Current visible reply.')).toBeVisible();
	await expect(page.getByText('Newer tool-only step 121.')).toHaveCount(0);
	await saveFocusPagingProof(page, testInfo, 'focus-on-1440x900');

	await page.setViewportSize({ width: 393, height: 852 });
	await expect(page.getByText('Current visible reply.')).toBeVisible();
	await saveFocusPagingProof(page, testInfo, 'focus-on-393x852');

	// MessageList.svelte pages older history on its own while the view cannot scroll (OlderHistoryRow.svelte keeps its
	// button only for when it cannot), so the older visible replies arrive without a click.
	await expect(page.getByText('Preload visible reply 2.')).toBeVisible();
	await saveFocusPagingProof(page, testInfo, 'load-older-adds-visible-item');

	await openSettings(page);
	await page.getByRole('button', { name: 'System', exact: true }).click();
	const focusToggle = page.getByRole('button', { name: 'Focus mode', exact: true });
	await focusToggle.click();
	await expect(focusToggle).toHaveAttribute('aria-pressed', 'false');
	await closeSettings(page);
	await openThread(page, title);
	await expect(page.getByText('Newer tool-only step 121.')).toBeVisible();
	await saveFocusPagingProof(page, testInfo, 'focus-off-unchanged');
});

test('@surface:focus-proof Focus mode folds background work and reveals it on demand at desktop and 393px', async ({ page, request }, testInfo) => {
	test.setTimeout(90_000);
	const title = `focus-proof-${Date.now()}`;
	const threadId = await createSession(request, title);
	const replyTurn = `focus-proof-reply-turn-${threadId}`;
	const awayTurn = `focus-proof-away-turn-${threadId}`;
	const humanId = await injectMessage(request, threadId, {
		role: 'user', content: 'Please check this.', metadata: {},
	}, TEST_TOKEN);
	await injectMessage(request, threadId, {
		role: 'assistant', content: 'Ordinary assistant answer.', reasoning: 'Ordinary assistant reasoning.',
		metadata: { turn_id: replyTurn, tool_calls: [{ id: 'focus-tool', name: 'read_file', arguments: {} }] },
	}, TEST_TOKEN);
	await injectMessage(request, threadId, {
		role: 'system', content: 'Passive event happened.',
		metadata: { type: 'context_event', event_type: 'webhook.github.pr_comment', summary: 'Passive event happened.' },
	}, TEST_TOKEN);
	await injectMessage(request, threadId, {
		role: 'assistant', content: 'Background analysis.', metadata: { turn_id: awayTurn },
	}, TEST_TOKEN);
	await injectMessage(request, threadId, {
		role: 'assistant', content: 'Important update for you.', metadata: { turn_id: awayTurn, for_user: true },
	}, TEST_TOKEN);
	await injectMessage(request, threadId, {
		role: 'assistant', content: 'More background work.', metadata: { turn_id: awayTurn },
	}, TEST_TOKEN);
	seedTerminalTurns(humanId, threadId, replyTurn, awayTurn);

	await page.setViewportSize({ width: 1280, height: 852 });
	await openThread(page, title);
	await ensureFocusMode(page);
	await openProofThread(page, title);
	await expect(page.getByText('Please check this.')).toBeVisible();
	await expect(page.getByText('Important update for you.')).toHaveCount(1);
	await expect(page.getByText('Ordinary assistant answer.')).toHaveCount(0);
	await expect(page.getByText('Ordinary assistant reasoning.')).toHaveCount(0);
	await expect(page.getByText('Background analysis.')).toHaveCount(0);
	await expect(page.getByText('More background work.')).toHaveCount(0);
	await expect(page.getByText('Passive event happened.')).toHaveCount(0);
	await expect(page.getByText('read_file')).toHaveCount(0);
	const fold = page.getByRole('button', { name: /Expand (Worked|Background work)/ }).first();
	await expect(fold).toBeVisible();
	await saveEvidenceScreenshot(page, testInfo, 'focus-on-desktop', { fullPage: false });
	await fold.click();
	await expect(page.getByText('Ordinary assistant answer.')).toBeVisible();
	await expect(page.getByText('Background analysis.')).toBeVisible();
	await expect(page.getByText('Passive event happened.')).toBeVisible();
	await saveEvidenceScreenshot(page, testInfo, 'focus-expanded-desktop', { fullPage: false });
	await page.getByRole('button', { name: /Collapse (Worked|Background work)/ }).first().click();
	await expect(page.getByText('Background analysis.')).toHaveCount(0);
	await expect(page.getByRole('button', { name: /Expand (Worked|Background work)/ }).first()).toBeVisible();
	await saveEvidenceScreenshot(page, testInfo, 'focus-collapsed-again-desktop', { fullPage: false });
	await page.getByRole('button', { name: /Expand (Worked|Background work)/ }).first().click();

	await openSettings(page);
	await page.getByRole('button', { name: 'System', exact: true }).click();
	const focusToggle = page.getByRole('button', { name: 'Focus mode', exact: true });
	await expect(focusToggle).toHaveAttribute('aria-pressed', 'true');
	expect(await page.locator('.theme-card').evaluate((element) =>
		element.nextElementSibling?.textContent?.includes('Focus mode') ?? false)).toBe(true);
	await saveEvidenceScreenshot(page, testInfo, 'focus-setting-desktop', { fullPage: false });
	await focusToggle.click();
	await expect(focusToggle).toHaveAttribute('aria-pressed', 'false');
	await closeSettings(page);
	await openProofThread(page, title);
	await expect(page.locator('.transcript-fold')).toHaveCount(0);
	await expect(page.getByText('Ordinary assistant answer.')).toBeVisible();
	await expect(page.getByText('Background analysis.')).toBeVisible();
	await expect(page.getByText('More background work.')).toBeVisible();

	await page.reload();
	await openProofThread(page, title);
	await expect(page.getByText('Background analysis.')).toBeVisible();
	await expect(page.locator('.transcript-fold')).toHaveCount(0);
	await page.setViewportSize({ width: 393, height: 852 });
	await openSettings(page);
	await page.getByRole('button', { name: 'System', exact: true }).click();
	await expect(focusToggle).toHaveAttribute('aria-pressed', 'false');
	await saveEvidenceScreenshot(page, testInfo, 'focus-setting-393', { fullPage: false });
	await focusToggle.click();
	await expect(focusToggle).toHaveAttribute('aria-pressed', 'true');
	await closeSettings(page);
	await openProofThread(page, title);
	await expect(page.getByText('Please check this.')).toBeVisible();
	await expect(page.getByText('Important update for you.')).toHaveCount(1);
	await expect(page.getByText('Ordinary assistant answer.')).toHaveCount(0);
	await expect(page.getByText('Ordinary assistant reasoning.')).toHaveCount(0);
	await expect(page.getByText('Background analysis.')).toHaveCount(0);
	await expect(page.getByRole('button', { name: /Expand (Worked|Background work)/ }).first()).toBeVisible();
	expect(await page.evaluate(() => window.innerWidth)).toBe(393);
	expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
	await saveEvidenceScreenshot(page, testInfo, 'focus-on-393', { fullPage: false });
	await page.getByRole('button', { name: /Expand (Worked|Background work)/ }).first().click();
	await expect(page.getByText('Background analysis.')).toBeVisible();
	expect(await page.evaluate(() => document.documentElement.scrollWidth <= window.innerWidth)).toBe(true);
	await saveEvidenceScreenshot(page, testInfo, 'focus-expanded-393', { fullPage: false });
});
