import { expect, test, type APIRequestContext } from '@playwright/test';
import { TEST_TOKEN } from '../support/corsia';
import { createComment, openLiveDocument, renderedCount, seedLiveDocument, serverText, typeAtDocumentEnd, type LiveDocument } from '../support/liveDocument';
import { liveFaultEvidence, liveFaultsEnabled, refuseLiveFaultOnce } from '../support/liveFault';
import {
	agentComment, countReceived, deviceCopies, injectForeignPositions, layouts, loseCommentCreate, markTexts, openLivePair, reviewerSession,
	storedCommentIds, suggest, watchPlacementReports, type CreateLoss,
} from '../support/liveConcurrency';

/**
 * Design 4.2 acceptance under late failures. Each fault is named: a comment create lost on the network,
 * an injected foreign-store copy of a real positions answer (a negative protocol fault the server never
 * sends), and the live-fault lane's one canonical rename refusal. Everything else stays real.
 */

async function storedRows(request: APIRequestContext, document: LiveDocument): Promise<Array<{ id: string; body: string; anchor_text?: string | null }>> {
	const listed = await request.get(`/api/artifacts/${document.artifactId}/comments`, { params: { token: TEST_TOKEN } });
	expect(listed.ok(), await listed.text()).toBeTruthy();
	return (await listed.json()).comments;
}

async function storedWithBody(request: APIRequestContext, document: LiveDocument, body: string): Promise<string[]> {
	return (await storedRows(request, document)).filter(row => row.body === body).map(row => row.id);
}

for (const [width, height, project] of layouts) {
	for (const loss of ['request', 'response'] as CreateLoss[]) {
		test(`@surface:live-document a comment create whose ${loss} is lost late keeps the draft and comment identities at ${width}`, async ({ browser, request }, testInfo) => {
			test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
			test.setTimeout(120_000);
			const stamp = `${width}-${loss}-${Date.now()}`;
			const existing = `existing quote ${stamp}`;
			const other = `other quote ${stamp}`;
			const document = await seedLiveDocument(request, `create-${stamp}`, `# Create ${stamp}\n\n${existing}\n\nselect target ${stamp}\n\n${other}\n`);
			await createComment(request, document, existing, `Existing ${stamp}`);
			const agent = await reviewerSession(request, stamp);
			const pair = await openLivePair(browser, document, width, height);
			try {
				const { reader, writer } = pair;
				const before = await storedCommentIds(request, document);
				expect(before).toHaveLength(1);
				const lost = await loseCommentCreate(reader, document.artifactId, loss);
				const target = reader.locator('.cm-line').filter({ hasText: `select target ${stamp}` });
				// Select the line's first word, as a reader commenting on it would (document-comment-scroll.spec.ts).
				await target.dblclick({ position: { x: 12, y: 8 } });
				await reader.locator('.sel-comment').click();
				const composer = reader.getByPlaceholder('Comment…');
				const draft = `Draft ${stamp}`;
				await composer.fill(draft);
				await reader.getByRole('button', { name: 'Comment', exact: true }).last().click();
				await lost.arrived;

				// While the create is in flight, a remote edit and another comment's load arrive.
				const remote = `remote ${stamp}`;
				await typeAtDocumentEnd(writer, remote);
				await agentComment(request, document, agent.id, other, `Other ${stamp}`);
				await expect(reader.locator('.cm-content')).toContainText(remote);
				// A lost response means the real server already committed the row, and its broadcast may mark it
				// before the create settles; that mark may sit only on the committed row's own anchor.
				let committedAnchor: string | null = null;
				if (loss === 'response') {
					await expect.poll(lost.committed, { message: 'the real server committed the create' }).not.toBeNull();
					const row = (await storedRows(request, document)).find(stored => stored.id === lost.committed());
					expect(row?.body).toBe(draft);
					committedAnchor = row?.anchor_text ?? null;
					expect(committedAnchor).toBe('select');
				}
				await expect.poll(async () => (await markTexts(reader)).filter(text => text !== committedAnchor)).toEqual([existing, other]);
				const marks = await markTexts(reader);
				expect(marks, 'every mark is a stored comment\'s anchor, in document order').toEqual(
					committedAnchor !== null && marks.includes(committedAnchor) ? [existing, committedAnchor, other] : [existing, other]);
				await expect(composer).toHaveValue(draft);
				lost.release();

				if (loss === 'request') {
					await expect(reader.getByText("Couldn't post the comment — try again")).toBeVisible();
					await expect(composer, 'a failed create keeps the draft').toHaveValue(draft);
					expect(await storedWithBody(request, document, draft)).toEqual([]);
					if (width <= 768) {
						await reader.getByRole('status').filter({ hasText: "Couldn't post the comment" }).getByRole('button', { name: 'Dismiss' }).click();
					}
					await reader.getByRole('button', { name: 'Comment', exact: true }).last().click();
				}
				await expect.poll(() => storedWithBody(request, document, draft), { message: 'exactly one stored comment carries the draft' }).toHaveLength(1);
				const [created] = await storedWithBody(request, document, draft);
				if (loss === 'response') {
					// The client reconciles by its own id; the real server's row is the one shown, never a second.
					expect(lost.committed()).toBe(created);
					expect(lost.clientId()).toBe(created);
					await expect(reader.getByText("Couldn't post the comment — try again")).toHaveCount(0);
				}
				// Save opens the single thread; data-comment-id decorates only whole-document list cards.
				await expect(reader.getByRole('region', { name: /^Comment by / }).filter({
					has: reader.getByText(draft, { exact: true }),
				})).toBeVisible();
				await expect(target.locator('.cm-comment-anchor'), 'the saved comment anchors on the selected word').toHaveText('select');
				const after = await storedCommentIds(request, document);
				expect(after).toHaveLength(3);
				expect(after).toEqual(expect.arrayContaining([...before, created]));
				await reader.screenshot({ path: testInfo.outputPath(`live-create-${loss}-lost-${width}.png`) });
			} finally {
				await pair.close();
			}
		});
	}

	test(`@surface:live-document an injected foreign-store positions answer is ignored and the real answer places the mark at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.setTimeout(120_000);
		const stamp = `${width}-${Date.now()}`;
		const placed = `placed quote ${stamp}`;
		const fresh = `new quote ${stamp}`;
		const document = await seedLiveDocument(request, `foreign-${stamp}`, `# Foreign ${stamp}\n\n${placed}\n\n${fresh}\n`);
		await createComment(request, document, placed, `Placed ${stamp}`);
		const agent = await reviewerSession(request, stamp);
		let fault!: Awaited<ReturnType<typeof injectForeignPositions>>;
		const pair = await openLivePair(browser, document, width, height, async context => {
			fault = await injectForeignPositions(context);
		});
		try {
			const { reader } = pair;
			const reports = watchPlacementReports(reader);
			const foreignSeen = await countReceived(reader, '"store_id":"foreign-');
			await expect.poll(() => markTexts(reader)).toEqual([placed]);
			await reader.getByRole('button', { name: '1 comment', exact: true }).click();
			const drawer = reader.getByRole('region', { name: 'Document comments', exact: true });
			fault.arm();
			await agentComment(request, document, agent.id, fresh, `Fresh ${stamp}`);
			await expect.poll(fault.injected, { timeout: 20_000 }).toBe(1);
			await expect.poll(foreignSeen, { message: 'the page received the injected copy' }).toBe(1);
			const card = drawer.locator('[data-comment-id]').filter({ hasText: `Fresh ${stamp}` });
			await expect(card.getByText('Finding its place…', { exact: true })).toBeVisible();
			// Let the page finish any digest the copy could have started before checking nothing moved.
			await reader.evaluate(async () => {
				await crypto.subtle.digest('SHA-256', new Uint8Array(8));
				await new Promise(resolve => setTimeout(resolve, 100));
			});
			expect(await markTexts(reader), 'the foreign copy placed nothing').toEqual([placed]);
			await expect(card.getByText('Finding its place…', { exact: true })).toBeVisible();

			fault.release();
			await expect.poll(() => markTexts(reader)).toEqual([placed, fresh]);
			await expect(card.getByText('Finding its place…', { exact: true })).toHaveCount(0);
			expect(reports()).toEqual([]);
			await reader.screenshot({ path: testInfo.outputPath(`live-foreign-positions-${width}.png`) });
		} finally {
			await pair.close();
		}
	});

	test(`@surface:live-document an Accept whose live flush is refused keeps the text and device copy and is not applied at ${width}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Run each layout in its matching browser mode.');
		test.skip(!liveFaultsEnabled(), 'Requires mock live-fault mode with freshly prepared roots.');
		test.setTimeout(120_000);
		const stamp = `${width}-${Date.now()}`;
		const document = await seedLiveDocument(request, `flush-refused-${stamp}`, `# Flush ${stamp}\n\nThe loose line ${stamp}.\n`);
		const reviewer = await reviewerSession(request, stamp);
		const loose = await suggest(request, document, reviewer.id, 'loose line', 'tight line', `Tighten ${stamp}`);
		const context = await browser.newContext({ viewport: { width, height } });
		try {
			const page = await context.newPage();
			await openLiveDocument(page, document);
			const typed = `typed ${stamp}`;
			await typeAtDocumentEnd(page, typed);
			await expect.poll(() => serverText(request, document), { timeout: 20_000 }).toContain(typed);
			await page.getByRole('button', { name: '1 comment', exact: true }).click();
			const row = page.getByRole('region', { name: 'Document comments', exact: true }).locator(`[data-comment-id="${loose}"]`);
			let decisions = 0;
			page.on('request', outgoing => { if (outgoing.url().includes(`/api/comments/${loose}/decision`)) decisions += 1; });

			// Accept flushes the live draft first (DocCommentSurface); that flush's file rename is refused once.
			await refuseLiveFaultOnce(request, document.artifactId, 'rename');
			await row.getByRole('button', { name: 'Accept', exact: true }).click();
			await expect(row.getByRole('alert')).toHaveText("Couldn't save the document before accepting this suggestion");
			expect(decisions, 'a refused flush never sends the decision').toBe(0);
			await expect(page.getByText(/^Not saved to the file:/)).toBeVisible();
			await expect(page.getByRole('button', { name: 'Copy text', exact: true })).toBeVisible();
			await expect(page.getByText("Changes aren't saved on this device", { exact: false })).toHaveCount(0);
			await expect(row.getByRole('region', { name: /^accepted suggestion by / })).toHaveCount(0);
			expect(await renderedCount(page, typed)).toBe(1);
			expect(await renderedCount(page, 'loose line')).toBe(1);
			const refused = await liveFaultEvidence(request, document.artifactId);
			expect(refused.live_text).toContain(typed);
			expect(refused.live_text).not.toContain('tight line');
			expect(refused.file_text).not.toContain('tight line');
			// The device copy is read back from this page's IndexedDB by the app's own reader, not inferred from
			// the absent device notice: one copy, holding exactly the live text the refused flush left.
			await expect.poll(() => deviceCopies(page, document.artifactId), { timeout: 10_000, message: 'the device copy holds the kept text' })
				.toEqual([refused.live_text]);
			await page.screenshot({ path: testInfo.outputPath(`live-accept-flush-refused-${width}.png`) });

			// The one refusal is spent: the same Accept now flushes, applies once and keeps the typed text.
			await row.getByRole('button', { name: 'Accept', exact: true }).click();
			await expect(row.getByRole('region', { name: /^accepted suggestion by / })).toBeVisible();
			expect(decisions).toBe(1);
			await expect.poll(() => renderedCount(page, 'tight line')).toBe(1);
			expect(await renderedCount(page, typed)).toBe(1);
			await expect.poll(async () => {
				const file = await serverText(request, document);
				return [file.split('tight line').length - 1, file.split('loose line').length - 1, file.split(typed).length - 1];
			}, { timeout: 20_000 }).toEqual([1, 0, 1]);
		} finally {
			await context.close();
		}
	});
}
