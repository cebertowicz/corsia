import fs from 'node:fs';
import path from 'node:path';
import { expect, type Page } from '@playwright/test';
import { test } from '../support/test';
import { createSession, TEST_TOKEN } from '../support/corsia';
import { installWorkspace, isolatedDb, openThread, setDebugVersionOverlay, setMockCorsiaAgentModel } from '../support/heldComments';

// The comment box names who gets a comment, under a new comment and under a reply, on a wide screen and on a phone.
const TEXT = '# Rollout plan\n\nThe team ships the commentable rollout plan in three rings, and each ring waits for the one before it.\n\n';
const WORD = 'commentable';
const auth = { token: TEST_TOKEN };

const settled = (page: Page) => page.evaluate(() => Promise.all(document.getAnimations().filter((a) => a.effect?.getComputedTiming().iterations !== Infinity).map((a) => a.finished.catch(() => undefined))).then(() => new Promise((r) => requestAnimationFrame(() => requestAnimationFrame(r)))));

/** The line, the field and Send as the person sees them: each box inside the viewport and inside the nearest scroller that holds the reply; also where that scroller stands. */
const visibleTogether = (page: Page) => page.evaluate(() => {
	const pick = (selector: string) => document.querySelector(selector) as HTMLElement | null;
	const line = pick('.ct-compose .rl');
	const field = pick('.ct-compose textarea.ct-input');
	const send = pick('.ct-compose [data-send-reply]');
	let scroller: HTMLElement | null = null;
	for (let parent = line?.parentElement ?? null; parent && !scroller; parent = parent.parentElement) if (/auto|scroll/.test(getComputedStyle(parent).overflowY) && parent.scrollHeight > parent.clientHeight + 1) scroller = parent;
	const view = scroller?.getBoundingClientRect();
	const top = Math.max(0, view?.top ?? 0);
	const bottom = Math.min(innerHeight, view?.bottom ?? innerHeight);
	const inside = (el: HTMLElement | null) => { if (!el) return false; const box = el.getBoundingClientRect(); return box.top >= top - 1 && box.bottom <= bottom + 1; };
	return { line: inside(line), field: inside(field), send: inside(send), scrollTop: scroller?.scrollTop ?? null, scrolls: !!scroller };
});

for (const view of [{ name: '1440', width: 1440, height: 900, phone: false }, { name: '393', width: 393, height: 852, phone: true }, { name: '852x393', width: 852, height: 393, phone: true }, { name: '393x440', width: 393, height: 440, phone: true }, { name: '320x340', width: 320, height: 340, phone: true }]) {
	test.describe(`recipient line ${view.name}`, () => {
		if (view.phone) test.use({ hasTouch: true, isMobile: true });
		test('a new comment and a reply say who gets them', async ({ page, request }, testInfo) => {
			test.skip(view.phone !== (testInfo.project.name === 'chromium-mobile'), 'other project');
			test.setTimeout(90_000);
			const made: string[] = [];
			try {
				await setMockCorsiaAgentModel(request);
				await setDebugVersionOverlay(request, false);
				const owner = await createSession(request, 'Docs lane'); made.push(owner);
				const reviewer = await createSession(request, 'Agent Substrate'); made.push(reviewer);
				const viewer = await createSession(request, 'Rollout review'); made.push(viewer);
				for (const [id, responsibility] of [[owner, 'Owns the rollout plan.'], [reviewer, 'Reviews the rollout plan.']]) {
					const done = await request.patch(`/api/agents/${id}`, { headers: { Authorization: `Bearer ${TEST_TOKEN}` }, data: { active: true, responsibility } });
					expect(done.ok(), await done.text()).toBeTruthy();
				}
				const roots: Array<{ kind: string; path: string }> = (await (await request.get('/api/files/tree', { params: auth })).json()).roots;
				const folder = path.join(roots.find((root) => root.kind === 'workspace')!.path, `recipient-${view.name}-${Date.now()}`);
				const docPath = path.join(folder, 'Rollout plan.md');
				fs.mkdirSync(folder, { recursive: true });
				const put = await request.put('/api/files/content', { params: auth, data: { path: docPath, text: TEXT, thread_id: owner, create: true } });
				expect(put.ok(), await put.text()).toBeTruthy();
				const artifactId = (await put.json()).artifact.id as string;

				// A thread an agent started: the reply goes to the owner and to that agent.
				const from = TEXT.indexOf(WORD);
				const posted = await request.post(`/api/artifacts/${artifactId}/comments`, { params: auth, data: { delivery_policy: 'immediate', body: 'Should ring two wait for the checks?', anchor_text: WORD, from_off: from, to_off: from + WORD.length } });
				expect(posted.ok(), await posted.text()).toBeTruthy();
				const rootId = (await posted.json()).comment.id as string;
				isolatedDb(['connection.execute("UPDATE doc_comments SET author_kind=\'agent\', author=?, author_thread_id=? WHERE id=?", ("Agent Substrate", sys.argv[2], sys.argv[3]))'], reviewer, rootId);

				await page.setViewportSize({ width: view.width, height: view.height });
				await installWorkspace(page, viewer, [{ artifactId, path: docPath, fileName: 'Rollout plan.md' }], 0, { visible: true, splitWidth: 700 });
				await openThread(page, viewer, 'Rollout review');
				await expect(page.locator('.cm-content')).toContainText(WORD);
				const press = (locator: ReturnType<Page['locator']>) => (view.phone ? locator.tap() : locator.click());

				// The reply box under the existing thread.
				await press(page.locator('.cm-comment-anchor').first());
				await settled(page);
				const line = page.locator('.ct-compose .rl');
				await expect(line).toBeVisible();
				await expect(line).toContainText('Goes to');
				await expect(line.locator('.rl-name')).toContainText(['Docs lane', 'Agent Substrate']);
				const reply = page.getByPlaceholder('Reply…');
				await reply.fill('Yes, ring two waits for ring one.');
				await expect(line).toBeVisible();

				// A four-line reply in a short popover or sheet: the line, the field and Send are in view together, and typing on does not move the scroller.
				await reply.fill('Yes, ring two waits for ring one.\nIt then runs the load tests.\nThe owner signs off after that.\nThen ring three starts.');
				await settled(page);
				await expect.poll(async () => { const seen = await visibleTogether(page); return seen.line && seen.field && seen.send; }, { message: 'line, field and Send in view together after a four-line reply' }).toBe(true);
				const before = await visibleTogether(page);
				await reply.pressSequentially(' Ok.');
				await settled(page);
				const after = await visibleTogether(page);
				expect(after.line && after.field && after.send, 'still together after typing on').toBe(true);
				expect(Math.abs((after.scrollTop ?? 0) - (before.scrollTop ?? 0)), 'typing on a line that already fits does not scroll').toBeLessThanOrEqual(30);

				// A new comment on other text: the box names the owner before anything is sent.
				await page.keyboard.press('Escape');
				await page.evaluate((word) => {
					const text = [...document.querySelectorAll('.cm-line')].find((l) => (l.textContent ?? '').includes(word));
					if (!text) throw new Error('Expected the sentence');
					const walker = document.createTreeWalker(text, NodeFilter.SHOW_TEXT);
					for (let node: Node | null = walker.nextNode(); node; node = walker.nextNode()) {
						const start = (node.textContent ?? '').indexOf(word);
						if (start < 0) continue;
						const range = document.createRange();
						range.setStart(node, start); range.setEnd(node, start + word.length);
						getSelection()?.removeAllRanges(); getSelection()?.addRange(range);
						document.dispatchEvent(new Event('selectionchange'));
						return;
					}
				}, 'rings');
				await expect(page.locator('.sel-comment')).toBeVisible({ timeout: 8000 });
				await press(page.locator('.sel-comment'));
				const box2 = page.getByPlaceholder('Comment, or @ to tag an agent');
				await expect(box2).toBeVisible();
				await box2.fill('Is ring three needed?');
				const newLine = page.locator('.composer .rl');
				await expect(newLine).toContainText('Goes to');
				await expect(newLine.locator('.rl-name')).toContainText(['Docs lane']);
			} finally {
				for (const id of made) await request.delete(`/api/threads/${id}`, { params: auth }).catch(() => undefined);
			}
		});
	});
}
