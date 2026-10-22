import { execFileSync } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { test } from '../support/test';
import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { createSession, loginByUrl, TEST_TOKEN } from '../support/corsia';
import { setFocusMode } from '../support/focusMode';

/**
 * The chip a message carries after it opened the shared browser in front of the person. The fixture writes the
 * assistant steps and the tool results that settle them the way the backend stores them, so the transcript is
 * read through the production display projection and the real components draw the chip.
 */
const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');
const backendDir = path.join(repoRoot, 'backend');
const python = () => process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python');

type Seed = { threadId: string; title: string };

function insertRow(threadId: string, role: string, content: string, metadata: Record<string, unknown>): string {
	const state = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !state) throw new Error('The chip fixture requires isolated E2E mock state');
	return execFileSync(python(), ['-c', [
		'import sys, uuid',
		'from infra.database import get_db',
		'from thread.transcript_order import draw_transcript_position',
		'thread, role, content, metadata = sys.argv[1:]',
		'db = get_db()',
		'seq = draw_transcript_position(db, thread)',
		'row_id = uuid.uuid4().hex',
		'db.execute("INSERT INTO messages (id, thread_id, role, content, metadata, created_at, seq) VALUES (?, ?, ?, ?, ?, datetime(\'now\'), ?)", (row_id, thread, role, content, metadata, seq))',
		'db.commit()',
		'print(row_id)',
	].join('\n'), threadId, role, content, JSON.stringify(metadata)], {
		cwd: backendDir,
		encoding: 'utf8',
		env: { ...process.env, CORSIA_TEST_MODE: '1', CORSIA_STATE_DIR: state, CORSIA_WORKSPACE: process.env.CORSIA_E2E_WORKSPACE ?? '' },
	}).trim();
}

type Call = ReturnType<typeof open>;

// A step that made tool calls, then the stored result of each, which is what settles them.
function insertStep(threadId: string, content: string, metadata: Record<string, unknown>, calls: Call[] = []): void {
	const stepId = insertRow(threadId, 'assistant', content, calls.length ? { ...metadata, llm_tool_calls: calls } : metadata);
	for (const call of calls) {
		insertRow(threadId, 'tool', 'opened', { assistant_step_id: stepId, tool_call_id: call.id });
	}
}

function open(url: string, showWindow: boolean) {
	return {
		id: `call-${Math.random().toString(36).slice(2, 9)}`,
		type: 'function',
		function: { name: 'browser_open', arguments: JSON.stringify(showWindow ? { url, show_window: true } : { url }) },
	};
}

async function seed(request: APIRequestContext, label: string): Promise<Seed> {
	const title = `Browser chip ${label} ${Math.random().toString(36).slice(2, 7)}`;
	const threadId = await createSession(request, title);
	const roots = await request.get('/api/files/tree', { params: { token: TEST_TOKEN } });
	const workspace = (await roots.json()).roots.find((root: { kind: string }) => root.kind === 'workspace').path;
	const name = `${title}.md`;
	const filePath = path.join(workspace, name);
	const saved = await request.put('/api/files/content', {
		params: { token: TEST_TOKEN },
		data: { path: filePath, text: '# Notes\n', thread_id: threadId, create: true },
	});
	expect(saved.ok(), await saved.text()).toBeTruthy();
	// A work step that opened a window, a headless step, then the update the person is meant to read: it names a file
	// and also opened a window of its own.
	insertStep(threadId, 'Checking the repository.', { turn_id: 't1' }, [open('https://www.example.org/start', true)]);
	insertStep(threadId, 'Reading the page without a window.', { turn_id: 't1' }, [open('https://headless.example.net/', false)]);
	insertStep(threadId, `I opened the repository for you and wrote [notes](${encodeURI(filePath)}).`, {
		turn_id: 't1', for_user: true,
	}, [open('https://www.github.com/cebertowicz/corsia', true)]);
	return { threadId, title };
}

// The way agents tell the person: the open is one step, and post_update is a separate said-to-you row after it.
async function seedOpenThenUpdate(request: APIRequestContext, label: string): Promise<Seed> {
	const title = `Browser chip ${label} ${Math.random().toString(36).slice(2, 7)}`;
	const threadId = await createSession(request, title);
	insertStep(threadId, 'Opening the repository.', { turn_id: 't1' }, [open('https://www.github.com/cebertowicz/corsia', true)]);
	insertStep(threadId, 'I put the repository in front of you.', { turn_id: 't1', for_user: true });
	return { threadId, title };
}

async function openTranscript(page: Page, seeded: Seed, width: number): Promise<void> {
	await page.setViewportSize({ width, height: width === 393 ? 852 : 900 });
	await loginByUrl(page, { waitForComposer: false });
	if (width === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
	await page.locator('.thread-item').filter({ hasText: seeded.title }).click();
}

async function screenshot(page: Page, name: string, within?: Locator): Promise<void> {
	const directory = process.env.CORSIA_E2E_EVIDENCE_DIR;
	if (!directory) return;
	fs.mkdirSync(directory, { recursive: true });
	await (within ?? page).screenshot({ path: path.join(directory, `${name}.png`), animations: 'disabled' });
}

const reopen = (page: Page, host: string) => page.getByRole('button', { name: host, exact: true });

// Focus is a persisted server setting shared by every later spec, so restore it even when a test fails.
test.afterEach(async ({ request }) => {
	await setFocusMode(request, true);
});

for (const width of [1440, 393] as const) {
	test.describe(`${width}`, () => {
		if (width === 393) test.use({ hasTouch: true, isMobile: true });

		test(`@surface:browser-chip Focus off: each visible open has a chip beside its file chip, a headless one has none (${width})`, async ({ page, request }, info) => {
			test.skip(width !== 393 && info.project.name === 'chromium-mobile', 'desktop is proved by the desktop project');
			await setFocusMode(request, false);
			const seeded = await seed(request, `off ${width}`);
			await openTranscript(page, seeded, width);

			const chip = reopen(page, 'github.com');
			await expect(chip).toBeVisible();
			await expect(reopen(page, 'example.org')).toBeVisible();
			await expect(page.locator('button.tile.chip').filter({ hasText: 'headless.example.net' })).toHaveCount(0);
			// It sits in the same strip as the file chip, after it, and is drawn as one.
			const strip = chip.locator('xpath=ancestor::ul[contains(@class, "chips")]');
			await expect(strip.locator('button.tile.chip')).toHaveCount(2);
			await expect(strip.locator('button.tile.chip').nth(1)).toHaveAttribute('title', 'Reopen the shared browser');
			await screenshot(page, `browser-chip-focus-off-${width}`);

			if (width === 1440) await chip.hover(); else await chip.focus();
			await screenshot(page, `browser-chip-${width === 1440 ? 'hover' : 'focus'}-${width}`);
			await screenshot(page, `browser-chip-strip-${width === 1440 ? 'hover' : 'focus'}-${width}`, strip);
			// A keyboard focus ring is drawn like a file chip's: inset, in the accent.
			await page.keyboard.press('Tab');
			await chip.focus();
			await page.keyboard.press('Shift+Tab');
			await page.keyboard.press('Tab');
			await expect(chip).toBeFocused();
			await screenshot(page, `browser-chip-strip-focus-ring-${width}`, strip);

			await chip.click();
			await expect(page.locator('.browser-leaf')).toBeVisible();
			await screenshot(page, `browser-chip-clicked-${width}`);
		});

		test(`@surface:browser-chip Focus on: the chip shows with the update the person reads, other opens stay inside their block (${width})`, async ({ page, request }, info) => {
			test.skip(width !== 393 && info.project.name === 'chromium-mobile', 'desktop is proved by the desktop project');
			await setFocusMode(request, true);
			const seeded = await seed(request, `on ${width}`);
			await openTranscript(page, seeded, width);

			const chip = reopen(page, 'github.com');
			await expect(chip).toBeVisible();
			await expect(chip.locator('xpath=ancestor::ul[contains(@class, "chips")]').locator('button.tile.chip')).toHaveCount(2);
			// The earlier step is work Focus tucks into its block, so its chip is not on screen until the block opens.
			await expect(reopen(page, 'example.org')).toHaveCount(0);
			await screenshot(page, `browser-chip-focus-on-${width}`);
			// Opening the block shows the step's own chip beside its words, as it does for a file the step names.
			await page.getByRole('button', { name: /Worked/ }).click();
			await expect(reopen(page, 'example.org')).toBeVisible();
			await screenshot(page, `browser-chip-focus-on-block-open-${width}`);
			await chip.click();
			await expect(page.locator('.browser-leaf')).toBeVisible();
			await screenshot(page, `browser-chip-focus-on-clicked-${width}`);
		});

		test(`@surface:browser-chip Focus on: an update sent after the open, in its own message, carries the chip (${width})`, async ({ page, request }, info) => {
			test.skip(width !== 393 && info.project.name === 'chromium-mobile', 'desktop is proved by the desktop project');
			await setFocusMode(request, true);
			const seeded = await seedOpenThenUpdate(request, `later ${width}`);
			await openTranscript(page, seeded, width);

			await expect(page.getByText('I put the repository in front of you.')).toBeVisible();
			const chip = reopen(page, 'github.com');
			await expect(chip).toHaveCount(1);
			await expect(chip).toBeVisible();
			await screenshot(page, `browser-chip-focus-on-later-update-${width}`);
			await chip.click();
			await expect(page.locator('.browser-leaf')).toBeVisible();
		});
	});
}
