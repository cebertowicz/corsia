import { mkdirSync } from 'node:fs';
import { join } from 'node:path';
import { test } from '../support/test';
import { expect, type Locator, type Page } from '@playwright/test';
import { createSession, TEST_TOKEN } from '../support/corsia';
import { demoteAgent } from '../support/sharedLiveDocument';
import { openComments } from '../support/commentDrawer';
import { installWorkspace, isolatedDb, openThread, seedTextDocument, setDebugVersionOverlay, setMockCorsiaAgentModel } from '../support/heldComments';

const auth = () => ({ Authorization: `Bearer ${TEST_TOKEN}` });

async function captureFrames(page: Page, visible: Locator, state: string): Promise<void> {
	const directory = process.env.TAG_FRAMES_DIR;
	if (!directory) return;
	const head = process.env.CORSIA_E2E_EXPECTED_HEAD;
	if (!head) throw new Error('Frame capture requires CORSIA_E2E_EXPECTED_HEAD');
	mkdirSync(directory, { recursive: true });
	for (const theme of ['light', 'dark'] as const) {
		await page.evaluate((value) => { document.documentElement.dataset.theme = value; }, theme);
		const shortHead = head.slice(0, 7);
		await expect(page.locator('.version-overlay')).toHaveText(new RegExp(`^\\s*b ${shortHead} · f ${shortHead} ·`));
		await expect(visible).toBeVisible();
		await expect.poll(() => visible.evaluate((element) => {
			let opacity = 1;
			for (let node: Element | null = element; node; node = node.parentElement) {
				opacity *= Number(getComputedStyle(node).opacity);
			}
			return opacity;
		})).toBe(1);
		await page.screenshot({ path: join(directory, `${page.viewportSize()?.width}-${theme}-${state}.png`), scale: 'css' });
	}
}

test('@flow:document-comments tagging an agent in a reply delivers only to participants', async ({ page, request }, testInfo) => {
	test.skip(process.env.CORSIA_E2E_MOCK !== '1', 'Requires isolated synthetic state');
	test.skip(testInfo.project.name !== 'chromium-desktop', 'One composed journey is enough');
	if (process.env.TAG_FRAMES_DIR) {
		const width = Number(process.env.TAG_FRAME_WIDTH);
		if (![375, 393, 1440].includes(width)) throw new Error('Frame width must be 375, 393 or 1440');
		await page.setViewportSize({ width, height: width === 1440 ? 900 : 852 });
		await setDebugVersionOverlay(request, true);
	}
	await setMockCorsiaAgentModel(request);
	const stamp = Date.now();
	const title = `Tag reply ${stamp}`;
	const owner = await createSession(request, title);
	const tagged = await createSession(request, `Tag recipient ${stamp}`);
	const other = await createSession(request, `Uninvolved ${stamp}`);
	try {
		for (const id of [tagged, other]) {
			const promoted = await request.patch(`/api/agents/${id}`, { headers: auth(), data: { active: true, responsibility: 'Reviews the document.' } });
			expect(promoted.ok(), await promoted.text()).toBeTruthy();
		}
		const doc = await seedTextDocument(request, owner, title, 'recipient');
		const root = await request.post(`/api/artifacts/${doc.artifactId}/comments`, {
			params: { token: TEST_TOKEN },
			data: { body: 'Please review this.', anchor_text: 'commentable', from_off: 2, to_off: 13, delivery_policy: 'immediate' },
		});
		expect(root.ok(), await root.text()).toBeTruthy();
		const rootId = (await root.json()).comment.id as string;
		await installWorkspace(page, owner, [doc]);
		await openThread(page, owner, title);
		await openComments(page);
		const card = page.locator(`[data-comment-id="${rootId}"]`).first();
		await expect(card).toBeVisible();
		const reply = card.getByPlaceholder('Reply…');
		await reply.fill('@Tag recipient');
		await captureFrames(page, page.getByRole('listbox', { name: 'Agents to tag' }), 'picker');
		await page.getByRole('option', { name: new RegExp(`Tag recipient ${stamp}`) }).click();
		await card.getByRole('button', { name: 'Send reply', exact: true }).click();
		const delivered = card.locator('.comment-mention').filter({ hasText: `@Tag recipient ${stamp}` });
		await expect(delivered).toBeVisible();
		await expect.poll(() => delivered.evaluate((element) => Number(getComputedStyle(element).fontWeight))).toBeGreaterThanOrEqual(600);
		await expect.poll(() => JSON.parse(isolatedDb([
			'rows = connection.execute("SELECT b.recipient_id, b.state, b.delivered_node_id FROM doc_feedback_batches b JOIN doc_feedback_batch_items i ON i.batch_id=b.id JOIN doc_comments c ON c.id=i.comment_id WHERE c.parent_id=? AND c.body LIKE ? ORDER BY b.recipient_id", (sys.argv[2], sys.argv[3])).fetchall()',
			'print(json.dumps(rows))',
		], rootId, `%Tag recipient ${stamp}%`))).toEqual(expect.arrayContaining([expect.arrayContaining([tagged, 'delivered', expect.any(String)])]));
		const recipients: string[] = JSON.parse(isolatedDb([
			'rows = connection.execute("SELECT DISTINCT b.recipient_id FROM doc_feedback_batches b JOIN doc_feedback_batch_items i ON i.batch_id=b.id JOIN doc_comments c ON c.id=i.comment_id WHERE c.parent_id=? AND c.body LIKE ?", (sys.argv[2], sys.argv[3])).fetchall()',
			'print(json.dumps([row[0] for row in rows]))',
		], rootId, `%Tag recipient ${stamp}%`));
		expect(recipients).toContain(tagged);
		expect(recipients).not.toContain(other);
		await expect.poll(() => JSON.parse(isolatedDb([
			'rows = connection.execute("SELECT DISTINCT m.thread_id FROM messages m JOIN doc_feedback_batches b ON b.recipient_id=m.thread_id AND json_valid(m.metadata) AND json_extract(m.metadata, \'$.node_id\')=b.delivered_node_id JOIN doc_feedback_batch_items i ON i.batch_id=b.id JOIN doc_comments c ON c.id=i.comment_id WHERE c.parent_id=? AND c.body LIKE ? AND m.thread_id IN (?, ?)", (sys.argv[2], sys.argv[3], sys.argv[4], sys.argv[5])).fetchall()',
			'print(json.dumps([row[0] for row in rows]))',
		], rootId, `%Tag recipient ${stamp}%`, tagged, other))).toEqual([tagged]);
		await captureFrames(page, delivered, 'delivered');

		// A cut and paste drops the picked identity, but the agent's full name typed after @ still tags it at send.
		await reply.fill('@Tag recipient');
		await page.getByRole('option', { name: new RegExp(`Tag recipient ${stamp}`) }).click();
		await reply.press('ControlOrMeta+A');
		await reply.press('ControlOrMeta+X');
		await reply.press('ControlOrMeta+V');
		await expect(reply).toHaveValue(`@Tag recipient ${stamp} `);
		const posted = page.waitForRequest((req) => req.method() === 'POST' && req.url().includes(`/api/artifacts/${doc.artifactId}/comments`));
		await card.getByRole('button', { name: 'Send reply', exact: true }).click();
		const payload = (await posted).postDataJSON();
		expect(payload.tagged_agent_ids).toEqual([tagged]);
		expect(payload.tagged_agent_mentions).toEqual([{ agent_id: tagged, start: 0, end: `@Tag recipient ${stamp}`.length }]);
		await expect(card.locator('.comment-mention').filter({ hasText: `@Tag recipient ${stamp}` })).toHaveCount(2);

		// A name typed without opening the list at all tags too; text that only starts like a name doesn't.
		const typed = page.waitForRequest((req) => req.method() === 'POST' && req.url().includes(`/api/artifacts/${doc.artifactId}/comments`));
		await reply.pressSequentially(`Over to @Tag recipient ${stamp}. Not @Tag recipient ${stamp}x`);
		await card.getByRole('button', { name: 'Send reply', exact: true }).click();
		expect((await typed).postDataJSON().tagged_agent_ids).toEqual([tagged]);
		await expect(card.locator('.comment-mention').filter({ hasText: `@Tag recipient ${stamp}` })).toHaveCount(3);
	} finally {
		await demoteAgent(request, tagged);
		await demoteAgent(request, other);
	}
});
