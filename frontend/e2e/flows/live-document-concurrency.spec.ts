import { expect, test, type Locator, type Page } from '@playwright/test';
import { createComment, renderedCount, seedLiveDocument, serverText, typeAtDocumentEnd, type LiveDocument } from '../support/liveDocument';
import {
	agentComment, holdPositionsAnswer, holdRequest, layouts, markTexts, openLivePair, recordMarkTexts, reviewerSession, suggest, textDigest, watchPlacementReports,
} from '../support/liveConcurrency';

/**
 * Design 4.2 acceptance on the real editor, marks and cards: concurrent typing, overlapping suggestions,
 * a positions answer outlived by a local edit, and a reply open while Accept is in flight. The document,
 * comments, suggestions and placement owners stay real; the only controls delay real traffic: one decision
 * request held before it reaches the server, or one real positions answer held, unmodified, on its way back.
 */

async function expectConverged(pages: Page[], document: LiveDocument, request: Parameters<typeof serverText>[0], once: string[], absent: string[] = []): Promise<void> {
	for (const page of pages) {
		for (const text of once) await expect.poll(() => renderedCount(page, text), { message: `${text} renders once` }).toBe(1);
		for (const text of absent) await expect.poll(() => renderedCount(page, text), { message: `${text} is gone` }).toBe(0);
	}
	await expect.poll(async () => {
		const file = await serverText(request, document);
		return [...once.map(text => file.split(text).length - 1), ...absent.map(text => file.split(text).length - 1)];
	}, { timeout: 20_000, message: 'the file holds the converged text' }).toEqual([...once.map(() => 1), ...absent.map(() => 0)]);
}

function drawerRow(page: Page, id: string): Locator {
	return page.getByRole('region', { name: 'Document comments', exact: true }).locator(`[data-comment-id="${id}"]`);
}

for (const [width, height, project] of layouts) {
	test(`@surface:live-document typing keeps its place while a remote edit and a comment load arrive at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(120_000);
		const stamp = `${width}-${Date.now()}`;
		const typed = ` typed while others work ${stamp} ${'and keeps going '.repeat(4)}`.trimEnd();
		const remote = `remote ${stamp}`;
		const quote = `late quote ${stamp}`;
		const document = await seedLiveDocument(request, `typing-${stamp}`, `# Typing ${stamp}\n\nalpha line ${stamp}\n\n${quote}\n\nomega line ${stamp}\n`);
		const pair = await openLivePair(browser, document, width, height);
		try {
			const { reader, writer } = pair;
			const reports = watchPlacementReports(reader);
			const editor = reader.locator('.cm-content');
			await expect(reader.locator('.cm-comment-anchor')).toHaveCount(0);
			await editor.locator('.cm-line').filter({ hasText: `alpha line ${stamp}` }).click();
			await reader.keyboard.press('End');
			let typingDone = false;
			const typing = reader.keyboard.type(typed, { delay: 40 }).then(() => { typingDone = true; });
			await typeAtDocumentEnd(writer, remote);
			// Create from the other reader's live selection, not offsets in the original seed text.
			await writer.locator('.cm-line').filter({ hasText: quote }).click();
			await writer.keyboard.press('Home');
			await writer.keyboard.press('Shift+End');
			await writer.locator('.sel-comment').click();
			await writer.getByPlaceholder('Comment…').fill(`Late ${stamp}`);
			await writer.getByRole('button', { name: 'Comment', exact: true }).last().click();
			await expect(editor).toContainText(remote);
			await expect(reader.getByRole('button', { name: '1 comment', exact: true })).toBeVisible();
			expect(typingDone, 'the remote edit and the comment load landed while the reader was still typing').toBe(false);
			await typing;

			// The caret never moved: the next key lands right after the typed text, and focus stayed in the editor.
			await reader.keyboard.type('!');
			await expect(editor.locator('.cm-line').filter({ hasText: `alpha line ${stamp}${typed}!` })).toHaveCount(1);
			await expect(editor).toBeFocused();
			await expectConverged([reader, writer], document, request, [`${typed}!`, remote]);
			// The comment load raced the typing and the remote edit; its mark lands on the quote alone.
			await expect.poll(() => markTexts(reader)).toEqual([quote]);
			await expect.poll(() => markTexts(writer)).toEqual([quote]);
			await expect(reader.getByText('Couldn’t find its place')).toHaveCount(0);
			expect(reports(), 'a clean tab never reports a placement mismatch').toEqual([]);
			await reader.screenshot({ path: testInfo.outputPath(`live-typing-comment-load-${width}.png`) });
		} finally {
			await pair.close();
		}
	});

	test(`@surface:live-document a positions answer outlived by a local edit is never applied to the changed text at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(120_000);
		const stamp = `${width}-${Date.now()}`;
		const quote = `held quote ${stamp}`;
		const shift = `shifted ${stamp} `;
		const document = await seedLiveDocument(request, `stale-${stamp}`, `# Stale ${stamp}\n\nlead line ${stamp}\n\n${quote}\n`);
		const agent = await reviewerSession(request, stamp);
		let hold!: Awaited<ReturnType<typeof holdPositionsAnswer>>;
		const pair = await openLivePair(browser, document, width, height, async context => { hold = await holdPositionsAnswer(context); });
		try {
			const { reader, writer } = pair;
			const reports = watchPlacementReports(reader);
			const shown = await recordMarkTexts(reader);
			const before = await serverText(request, document);
			hold.arm();
			await agentComment(request, document, agent.id, quote, `Held ${stamp}`);
			await expect.poll(() => hold.held()?.seq ?? null, { timeout: 20_000, message: 'the real answer to the comment load is held' }).not.toBeNull();
			const answer = hold.held()!;
			expect(answer.store_id).toBeTruthy();
			expect(hold.asks(), 'it answers an ask this page sent').toContain(answer.seq);
			expect(answer.digest, 'the held answer describes the text before the edit').toBe(await textDigest(before));
			expect(await markTexts(reader)).toEqual([]);

			// While the answer is held, the reader changes the real text ahead of the quote, shifting every offset.
			const editor = reader.locator('.cm-content');
			await editor.locator('.cm-line').filter({ hasText: `lead line ${stamp}` }).click();
			await reader.keyboard.press('Home');
			await reader.keyboard.type(shift);
			await expect(editor.locator('.cm-line').filter({ hasText: `${shift}lead line ${stamp}` })).toHaveCount(1);
			await expect.poll(() => serverText(request, document), { timeout: 20_000 }).toContain(`${shift}lead line`);
			expect(await textDigest(await serverText(request, document)), 'the real text no longer matches the held answer').not.toBe(answer.digest);
			expect(await markTexts(reader)).toEqual([]);

			const asked = hold.asks().length;
			hold.release();
			await expect.poll(() => hold.asks().slice(asked).filter(seq => seq > answer.seq!).length, {
				timeout: 20_000, message: 'the page asks again instead of using the outlived answer' }).toBeGreaterThan(0);
			await expect.poll(() => markTexts(reader), { timeout: 20_000 }).toEqual([quote]);
			const everShown = await shown();
			expect(everShown.length).toBeGreaterThan(0);
			expect(everShown.filter(text => !text || !quote.includes(text)), 'no mark was ever drawn on other text').toEqual([]);
			expect(reports(), 'a tab with local edits never reports a placement mismatch').toEqual([]);
			await expectConverged([reader, writer], document, request, [`${shift}lead line ${stamp}`, quote]);
			await expect.poll(() => markTexts(writer)).toEqual([quote]);
			await reader.screenshot({ path: testInfo.outputPath(`live-outlived-positions-${width}.png`) });
		} finally {
			await pair.close();
		}
	});

	test(`@surface:live-document overlapping suggestions accept and reject without duplicate text at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(120_000);
		const stamp = `${width}-${Date.now()}`;
		const sentence = `The quick brown fox jumps ${stamp}.`;
		const document = await seedLiveDocument(request, `overlap-${stamp}`, `# Overlap ${stamp}\n\n${sentence}\n\nwriter line ${stamp}\n`);
		const reviewer = await reviewerSession(request, stamp);
		const red = await suggest(request, document, reviewer.id, 'quick brown fox', 'quick red fox', `Red ${stamp}`);
		const dog = await suggest(request, document, reviewer.id, 'brown fox jumps', 'brown dog jumps', `Dog ${stamp}`);
		const pair = await openLivePair(browser, document, width, height);
		try {
			const { reader, writer } = pair;
			const remote = `remote ${stamp}`;
			await typeAtDocumentEnd(writer, remote);
			await expect(reader.locator('.cm-content')).toContainText(remote);
			await reader.getByRole('button', { name: '2 comments', exact: true }).click();
			await drawerRow(reader, red).getByRole('button', { name: 'Accept', exact: true }).click();
			await expect(drawerRow(reader, red).getByRole('region', { name: /^accepted suggestion by / })).toBeVisible();
			// The accepted replacement removed the overlapping suggestion's quote, so it can no longer be accepted.
			await expect(drawerRow(reader, dog)).toContainText('The text this suggestion replaces has changed.');
			await expect(drawerRow(reader, dog).getByRole('button', { name: 'Accept', exact: true })).toBeDisabled();
			await drawerRow(reader, dog).getByRole('button', { name: 'Reject', exact: true }).click();
			await expect(drawerRow(reader, dog).getByRole('region', { name: /^rejected suggestion by / })).toBeVisible();
			await expectConverged([reader, writer], document, request, [`The quick red fox jumps ${stamp}.`, remote], ['brown', 'dog jumps']);
			await reader.screenshot({ path: testInfo.outputPath(`live-overlap-decided-${width}.png`) });
		} finally {
			await pair.close();
		}
	});

	test(`@surface:live-document accept-all over overlapping suggestions applies one and skips the other at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(120_000);
		const stamp = `${width}-${Date.now()}`;
		const document = await seedLiveDocument(request, `overlap-all-${stamp}`, `# Overlap all ${stamp}\n\nThe quick brown fox jumps ${stamp}.\n`);
		const reviewer = await reviewerSession(request, stamp);
		const red = await suggest(request, document, reviewer.id, 'quick brown fox', 'quick red fox', `Red ${stamp}`);
		const dog = await suggest(request, document, reviewer.id, 'brown fox jumps', 'brown dog jumps', `Dog ${stamp}`);
		const pair = await openLivePair(browser, document, width, height);
		try {
			const { reader, writer } = pair;
			await reader.getByRole('button', { name: '2 comments', exact: true }).click();
			const drawer = reader.getByRole('region', { name: 'Document comments', exact: true });
			await drawer.getByRole('button', { name: `Accept all from ${reviewer.title}`, exact: true }).click();
			await expect(drawerRow(reader, red).getByRole('region', { name: /^accepted suggestion by / })).toBeVisible();
			await expect(drawer.getByText('1 skipped', { exact: true })).toBeVisible();
			await expect(drawerRow(reader, dog).getByRole('region', { name: /^accepted suggestion by / })).toHaveCount(0);
			await expectConverged([reader, writer], document, request, [`The quick red fox jumps ${stamp}.`], ['brown', 'dog jumps']);
		} finally {
			await pair.close();
		}
	});

	test(`@surface:live-document a reply draft keeps its value, focus and selection while Accept is in flight during a remote edit at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(120_000);
		const stamp = `${width}-${Date.now()}`;
		const document = await seedLiveDocument(request, `reply-accept-${stamp}`, `# Reply ${stamp}\n\nThe loose line ${stamp}.\n\nreply anchor ${stamp}\n`);
		const reviewer = await reviewerSession(request, stamp);
		const loose = await suggest(request, document, reviewer.id, 'loose line', 'tight line', `Tighten ${stamp}`);
		await createComment(request, document, `reply anchor ${stamp}`, `Reply ${stamp}`);
		const pair = await openLivePair(browser, document, width, height);
		try {
			const { reader, writer } = pair;
			await reader.getByRole('button', { name: '2 comments', exact: true }).click();
			const drawer = reader.getByRole('region', { name: 'Document comments', exact: true });
			const replyId = await drawer.locator('[data-comment-id]').filter({ hasText: `Reply ${stamp}` }).getAttribute('data-comment-id');
			expect(replyId).toBeTruthy();
			const reply = drawerRow(reader, replyId!).getByPlaceholder('Reply…');
			const replyState = () => reply.evaluate(element => {
				const input = element as HTMLInputElement;
				return { value: input.value, selection: [input.selectionStart, input.selectionEnd], focused: input.ownerDocument.activeElement === input };
			});
			const draft = { value: `draft ${stamp}`, selection: [2, 5], focused: true };

			const held = await holdRequest(reader, `**/api/comments/${loose}/decision**`);
			await drawerRow(reader, loose).getByRole('button', { name: 'Accept', exact: true }).click();
			await held.arrived;
			await expect(drawerRow(reader, loose).getByRole('button', { name: 'Accepting…', exact: true })).toHaveAttribute('aria-busy', 'true');
			await reply.fill(draft.value);
			await reply.evaluate(element => (element as HTMLInputElement).setSelectionRange(2, 5));
			expect(await replyState()).toEqual(draft);

			const remote = `remote ${stamp}`;
			await typeAtDocumentEnd(writer, remote);
			await expect(reader.locator('.cm-content')).toContainText(remote);
			expect(await replyState(), 'a remote edit never takes the reply draft or its focus').toEqual(draft);

			held.release();
			await expect(drawerRow(reader, loose).getByRole('region', { name: /^accepted suggestion by / })).toBeVisible();
			await expect(reader.locator('.cm-content')).toContainText(`The tight line ${stamp}.`);
			await expect.poll(replyState, { message: 'the settled Accept and its comments load leave the draft alone' }).toEqual(draft);
			await expectConverged([reader, writer], document, request, [`The tight line ${stamp}.`, remote], ['loose line']);
			expect(await replyState()).toEqual(draft);
			await reader.screenshot({ path: testInfo.outputPath(`live-reply-during-accept-${width}.png`) });
		} finally {
			await pair.close();
		}
	});
}
