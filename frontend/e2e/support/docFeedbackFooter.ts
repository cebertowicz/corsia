import { execFileSync } from 'node:child_process';
import path from 'node:path';
import { fileURLToPath } from 'node:url';
import { expect, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { TEST_TOKEN } from './corsia';

const repoRoot = path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../..');

/** The server delivers an open person batch five seconds after its last item; push that out so a spec can read the unsent state. */
function holdOpenBatches(): void {
	const stateRoot = process.env.CORSIA_E2E_STATE_DIR;
	if (process.env.CORSIA_E2E_MOCK !== '1' || !stateRoot) throw new Error('Holding a batch open requires isolated E2E mock state');
	execFileSync(process.env.CORSIA_E2E_BACKEND_PYTHON ?? path.join(repoRoot, '.venv/bin/python'), [
		'-c', [
			'import sqlite3, sys',
			'from datetime import datetime, timedelta, timezone',
			'connection = sqlite3.connect(sys.argv[1])',
			'later = (datetime.now(timezone.utc) + timedelta(hours=1)).isoformat()',
			'connection.execute("UPDATE doc_feedback_batches SET closes_at=? WHERE state=\'open\' AND author_kind=\'user\'", (later,))',
			'connection.commit()',
			'connection.close()',
		].join('\n'), path.join(stateRoot, 'corsia.db'),
	], { cwd: repoRoot, encoding: 'utf8' });
}

/**
 * A person's comment is delivered as it is made, so no unsent state follows it. Re-opening a resolved comment is the
 * person action that still waits for the person to stop: resolve then re-open a comment to leave one unsent message.
 */
export async function leaveUnsent(request: APIRequestContext, commentId: string): Promise<void> {
	for (const resolved of [true, false]) {
		const patched = await request.patch(`/api/comments/${commentId}`, { params: { token: TEST_TOKEN }, data: { resolved } });
		expect(patched.ok(), await patched.text()).toBeTruthy();
	}
	holdOpenBatches();
}

/** Leave every top-level comment a person made on a document unsent, as `leaveUnsent` does for one. */
export async function leaveUserCommentsUnsent(request: APIRequestContext, artifactId: string): Promise<void> {
	const listed = await request.get(`/api/artifacts/${artifactId}/comments`, { params: { token: TEST_TOKEN } });
	expect(listed.ok(), await listed.text()).toBeTruthy();
	const comments = (await listed.json()).comments as Array<{ id: string; author_kind: string; parent_id: string | null }>;
	for (const comment of comments) if (comment.author_kind === 'user' && !comment.parent_id) await leaveUnsent(request, comment.id);
}

/** Open the comments drawer from the header pill ("N open threads, …") and return its unsent-messages footer. */
export async function openUnsentFooter(page: Page): Promise<Locator> {
	await page.getByRole('button', { name: /^\d+ open threads?\b/ }).click();
	return page.getByRole('region', { name: 'Document comments' }).locator('[data-drawer-unsent]');
}

/**
 * The feedback Send controls and sending countdowns in the document header that a person could see: text with a box
 * larger than 1 px under no hidden, clipped or collapsed ancestor. Playwright's `toBeVisible` ignores `clip`, so a
 * clipped Send once passed as visible; Send now lives in the comments drawer's footer instead.
 */
export function visibleHeaderSends(page: Page): Promise<string[]> {
	return page.evaluate(() => {
		const header = document.querySelector('.document-header');
		if (!header) return [];
		const hidden = (element: Element) => {
			for (let node: Element | null = element; node; node = node.parentElement) {
				const style = getComputedStyle(node);
				if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) return true;
				if (style.position === 'absolute' && style.clip !== 'auto') return true;
				const box = node.getBoundingClientRect();
				if (style.overflow !== 'visible' && (box.width <= 1 || box.height <= 1)) return true;
			}
			return false;
		};
		const found: string[] = [];
		const walker = document.createTreeWalker(header, NodeFilter.SHOW_TEXT);
		for (let text = walker.nextNode(); text; text = walker.nextNode()) {
			if (!/\bSend \d+ comments?\b|\bSend now\b|\bSending to\b/.test(text.textContent ?? '')) continue;
			const range = document.createRange();
			range.selectNodeContents(text);
			const box = range.getBoundingClientRect();
			if (box.width > 1 && box.height > 1 && !hidden(text.parentElement!)) found.push(text.textContent!.trim());
		}
		return found;
	});
}
