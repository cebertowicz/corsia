import { expect, test, type BrowserContext } from '@playwright/test';
import { holdRequest, openLivePair, textDigest, watchPlacementReports } from '../support/liveConcurrency';
import { seedLiveDocument, serverText } from '../support/liveDocument';
import {
	beginComment, commentWire, expectCorrelation, expectOneComment, expectToastLayout, insertPrefix,
	observeCreates, openTwoDocuments, outsideClose, selectLine, storedComments, submitButton, switchDocument,
	type CommentWire,
} from '../support/liveCommentProof';

const layouts = [[1440, 960, 'chromium-desktop'], [393, 852, 'chromium-mobile']] as const;
const refusalText = 'The document changed before your comment could be attached. Your draft is kept. Try again.';

async function prepare(context: BrowserContext, theme: 'light' | 'dark'): Promise<CommentWire> {
	await context.addInitScript(value => localStorage.setItem('corsia_theme', value), theme);
	return commentWire(context);
}

// Actual editor/server acceptance: only transport timing is controlled. No target answers are mocked.
for (const [width, height, project] of layouts) for (const theme of ['light', 'dark'] as const) {
	const label = `${width} ${theme}`;
	test(`@surface:live-document @comment-proof selection before Comment survives a remote prefix at ${label}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Matching Chromium layout only.');
		test.setTimeout(120_000);
		const stamp = `${width}-${theme}-${Date.now()}`, quote = `selected identity ${stamp}`, body = `Original comment body ${stamp}`;
		const document = await seedLiveDocument(request, `selection-proof-${stamp}`, `lead ${stamp}\n${quote}\ntail ${stamp}\n`);
		let wire!: CommentWire;
		const pair = await openLivePair(browser, document, width, height, async context => { wire = await prepare(context, theme); });
		try {
			const { reader, writer } = pair, creates = observeCreates(reader, document.artifactId), reports = watchPlacementReports(reader);
			await expect(reader.locator('html')).toHaveAttribute('data-theme', theme);
			await selectLine(reader, quote);
			const prefix = `remote 𝄞 prefix ${stamp}\n`;
			await insertPrefix(writer, prefix);
			await expect(reader.locator('.cm-content')).toContainText(prefix.trim());
			await expect.poll(() => serverText(request, document), { timeout: 20_000 }).toBe(prefix + document.body);
			// The selection was captured before the remote change and before opening the composer.
			await reader.locator('.sel-comment').click();
			await reader.getByPlaceholder('Comment…').fill(body);
			await submitButton(reader).click();
			await expect(reader.getByPlaceholder('Comment…')).toHaveCount(0);
			expect(creates).toHaveLength(1);
			const payload = creates[0], file = await serverText(request, document);
			expect(payload).toMatchObject({ body, anchor_text: quote, from_off: file.indexOf(quote), to_off: file.indexOf(quote) + quote.length });
			expect(file.length, 'fixture distinguishes UTF-16 units from code points').toBeGreaterThan([...file].length);
			expect(expectCorrelation(wire, payload, document.artifactId)).toMatchObject({ digest: await textDigest(file), text_length_utf16: file.length });
			await expectOneComment(request, document, [reader, writer], quote, body, file.indexOf(quote), payload.client_id);
			await expect(reader.getByText(body, { exact: true })).toHaveCount(1);
			expect(reports()).toEqual([]);
			await reader.screenshot({ path: testInfo.outputPath(`selection-prefix-${width}-${theme}.png`), animations: 'disabled' });
		} finally { await pair.close(); }
	});

	test(`@surface:live-document @comment-proof the selected text stays highlighted while the composer is open and follows a remote prefix at ${label}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Matching Chromium layout only.');
		test.setTimeout(120_000);
		const stamp = `${width}-${theme}-${Date.now()}`, quote = `pending identity ${stamp}`;
		const document = await seedLiveDocument(request, `pending-highlight-${stamp}`, `lead ${stamp}\n${quote}\ntail ${stamp}\n`);
		const pair = await openLivePair(browser, document, width, height, async context => { await prepare(context, theme); });
		try {
			const { reader, writer } = pair, pending = reader.locator('.cm-comment-pending');
			await expect(reader.locator('html')).toHaveAttribute('data-theme', theme);
			await beginComment(reader, quote, `typing ${stamp}`);
			await expect(pending).toHaveText(quote);
			await expect(pending).toBeVisible();
			const prefix = `remote prefix ${stamp}\n`;
			await insertPrefix(writer, prefix);
			await expect(reader.locator('.cm-content')).toContainText(prefix.trim());
			await expect(pending).toHaveText(quote);
			await reader.screenshot({ path: testInfo.outputPath(`pending-highlight-${width}-${theme}.png`), animations: 'disabled' });
			await outsideClose(reader);
			await expect(pending).toHaveCount(0);
		} finally { await pair.close(); }
	});

	test(`@surface:live-document @comment-proof equal text replacement refuses old identity and reselection carries body at ${label}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Matching Chromium layout only.');
		test.setTimeout(120_000);
		const stamp = `${width}-${theme}-${Date.now()}`, quote = `equal target ${stamp}`, body = `Keep this exact body ${stamp}`;
		const document = await seedLiveDocument(request, `identity-proof-${stamp}`, `lead ${stamp}\n${quote}\ntail ${stamp}\n`);
		let wire!: CommentWire;
		const pair = await openLivePair(browser, document, width, height, async context => { wire = await prepare(context, theme); });
		try {
			const { reader, writer } = pair, creates = observeCreates(reader, document.artifactId);
			await expect(reader.locator('html')).toHaveAttribute('data-theme', theme);
			await beginComment(reader, quote, body);
			await selectLine(writer, quote);
			await writer.keyboard.press('Backspace');
			await expect(reader.locator('.cm-content')).not.toContainText(quote);
			await expect(submitButton(reader)).toBeDisabled();
			await writer.keyboard.insertText(quote);
			await expect(reader.locator('.cm-content')).toContainText(quote);
			await expect.poll(() => serverText(request, document), { timeout: 20_000 }).toBe(document.body);
			await expect(submitButton(reader)).toBeDisabled();
			await expect(reader.getByPlaceholder('Comment…')).toHaveValue(body);
			const explanation = await submitButton(reader).getAttribute('aria-describedby');
			expect(explanation).toBeTruthy();
			await expect(reader.locator(`[id="${explanation}"]`)).toHaveText('The text you selected changed or was removed. Select the text again; your draft will carry over.');
			expect(creates).toEqual([]);
			expect(await storedComments(request, document)).toEqual([]);
			await reader.screenshot({ path: testInfo.outputPath(`equal-identity-refusal-${width}-${theme}.png`), animations: 'disabled' });
			await outsideClose(reader);
			await beginComment(reader, quote);
			await expect(reader.getByPlaceholder('Comment…')).toHaveValue(body);
			await expect(submitButton(reader)).toBeEnabled();
			await submitButton(reader).click();
			await expect(reader.getByPlaceholder('Comment…')).toHaveCount(0);
			expect(creates).toHaveLength(1);
			expectCorrelation(wire, creates[0], document.artifactId);
			await expectOneComment(request, document, [reader, writer], quote, body, document.body.indexOf(quote), creates[0].client_id);
			await expect(reader.getByText(body, { exact: true })).toHaveCount(1);
		} finally { await pair.close(); }
	});

	test(`@surface:live-document @comment-proof a remote edit between capture and a delayed create does not refuse it and the comment keeps its original text at ${label}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Matching Chromium layout only.');
		test.setTimeout(120_000);
		const stamp = `${width}-${theme}-${Date.now()}`, quote = `wire target ${stamp}`, body = `Delayed original body ${stamp}`;
		const document = await seedLiveDocument(request, `wire-proof-${stamp}`, `lead ${stamp}\n${quote}\ntail ${stamp}\n`);
		let wire!: CommentWire;
		const pair = await openLivePair(browser, document, width, height, async context => { wire = await prepare(context, theme); });
		let hold: Awaited<ReturnType<typeof holdRequest>> | undefined;
		try {
			const { reader, writer } = pair, creates = observeCreates(reader, document.artifactId);
			await expect(reader.locator('html')).toHaveAttribute('data-theme', theme);
			hold = await holdRequest(reader, `**/api/artifacts/${document.artifactId}/comments*`);
			await beginComment(reader, quote, body);
			await submitButton(reader).click();
			await hold.arrived;
			await expect(reader.locator('.composer').getByRole('button', { name: 'Posting…', exact: true })).toBeDisabled();
			await expect(reader.getByPlaceholder('Comment…')).toHaveValue(body);
			expect(await storedComments(request, document)).toEqual([]);
			// The capture has already reserved its range on the server; a remote edit lands before the POST does.
			const prefix = `intervening edit ${stamp}\n`;
			await insertPrefix(writer, prefix);
			await expect.poll(() => serverText(request, document), { timeout: 20_000 }).toBe(prefix + document.body);
			await expect(reader.locator('.cm-content')).toContainText(prefix.trim());
			const created = reader.waitForResponse(response => new URL(response.url()).pathname === `/api/artifacts/${document.artifactId}/comments` && response.request().method() === 'POST');
			hold.release();
			expect((await created).status(), 'the server consumes the reservation instead of refusing the older state').toBe(200);
			await expect(reader.getByPlaceholder('Comment…')).toHaveCount(0);
			await expect(reader.getByRole('status').filter({ hasText: refusalText })).toHaveCount(0);
			expect(creates).toHaveLength(1);
			const payload = creates[0], file = await serverText(request, document), original = document.body.indexOf(quote);
			// The reservation carries the state the browser captured, which is no longer the document's state.
			expect(file).not.toBe(document.body);
			expect(payload).toMatchObject({ body, anchor_text: quote, from_off: original, to_off: original + quote.length });
			expect(expectCorrelation(wire, payload, document.artifactId)).toMatchObject({ digest: await textDigest(document.body), text_length_utf16: document.body.length });
			// Stored once, on the originally selected text, which the remote prefix moved.
			expect(file.indexOf(quote)).toBe(original + prefix.length);
			await expectOneComment(request, document, [reader, writer], quote, body, original + prefix.length, payload.client_id);
			await expect(reader.getByText(body, { exact: true })).toHaveCount(1);
			await reader.screenshot({ path: testInfo.outputPath(`reservation-after-remote-edit-${width}-${theme}.png`), animations: 'disabled' });
		} finally { hold?.release(); await pair.close(); }
	});

	test(`@surface:live-document @comment-proof a remote edit inside the reserved text refuses the create with its toast and stores nothing at ${label}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Matching Chromium layout only.');
		test.setTimeout(120_000);
		const stamp = `${width}-${theme}-${Date.now()}`, quote = `inside target ${stamp}`, body = `Refused body ${stamp}`;
		const document = await seedLiveDocument(request, `inside-proof-${stamp}`, `lead ${stamp}\n${quote}\ntail ${stamp}\n`);
		let wire!: CommentWire;
		const pair = await openLivePair(browser, document, width, height, async context => { wire = await prepare(context, theme); });
		let hold: Awaited<ReturnType<typeof holdRequest>> | undefined;
		try {
			const { reader, writer } = pair, creates = observeCreates(reader, document.artifactId);
			await expect(reader.locator('html')).toHaveAttribute('data-theme', theme);
			hold = await holdRequest(reader, `**/api/artifacts/${document.artifactId}/comments*`);
			await beginComment(reader, quote, body);
			await submitButton(reader).click();
			await hold.arrived;
			await expect(reader.getByPlaceholder('Comment…')).toHaveValue(body);
			// Three characters into the selected line is inside the reserved range, not at either edge.
			await selectLine(writer, quote);
			await writer.keyboard.press('Home');
			for (let step = 0; step < 3; step++) await writer.keyboard.press('ArrowRight');
			await writer.keyboard.insertText('ZZ');
			const edited = quote.slice(0, 3) + 'ZZ' + quote.slice(3);
			await expect.poll(() => serverText(request, document), { timeout: 20_000 }).toBe(document.body.replace(quote, edited));
			await expect(reader.locator('.cm-content')).toContainText(edited);
			const refused = reader.waitForResponse(response => new URL(response.url()).pathname === `/api/artifacts/${document.artifactId}/comments` && response.request().method() === 'POST');
			hold.release();
			expect((await refused).status(), 'the reserved range no longer holds the selected text').toBe(409);
			const toast = reader.getByRole('status').filter({ hasText: refusalText });
			await expect(toast).toBeVisible();
			await expectToastLayout(reader, toast);
			await expect(reader.getByPlaceholder('Comment…')).toHaveValue(body);
			expect(await storedComments(request, document)).toEqual([]);
			expect(creates).toHaveLength(1);
			expectCorrelation(wire, creates[0], document.artifactId);
			await reader.screenshot({ path: testInfo.outputPath(`reservation-refusal-toast-${width}-${theme}.png`), animations: 'disabled' });
		} finally { hold?.release(); await pair.close(); }
	});

	test(`@surface:live-document @comment-proof closing cancels capture, Cancel discards, and document switches never carry a body at ${label}`, async ({ browser, request }, testInfo) => {
		test.skip(testInfo.project.name !== project, 'Matching Chromium layout only.');
		test.setTimeout(120_000);
		const stamp = `${width}-${theme}-${Date.now()}`, quote = `scope target ${stamp}`, otherQuote = `other target ${stamp}`, body = `Scoped body ${stamp}`;
		const document = await seedLiveDocument(request, `scope-proof-${stamp}`, `${quote}\n`);
		const other = await seedLiveDocument(request, `scope-other-${stamp}`, `${otherQuote}\n`);
		const context = await browser.newContext({ viewport: { width, height } });
		const wire = await prepare(context, theme), reader = await context.newPage();
		try {
			const creates = observeCreates(reader, document.artifactId), otherCreates = observeCreates(reader, other.artifactId);
			await openTwoDocuments(reader, document, other);
			await expect(reader.locator('html')).toHaveAttribute('data-theme', theme);
			await beginComment(reader, quote, body);
			wire.arm();
			await submitButton(reader).click();
			await expect.poll(() => wire.held(), { timeout: 20_000 }).not.toBeNull();
			const closedAsk = wire.held()!;
			await outsideClose(reader);
			wire.release();
			await beginComment(reader, quote);
			await expect(reader.getByPlaceholder('Comment…')).toHaveValue(body);
			await expect(submitButton(reader)).toBeEnabled();
			expect(creates).toEqual([]);
			await reader.locator('.composer').getByRole('button', { name: 'Cancel', exact: true }).click();
			// On a narrow canvas the cancelled composer remains visible for its drawer outro.
			await expect(reader.getByPlaceholder('Comment…')).toHaveCount(0);
			await beginComment(reader, quote);
			await expect(reader.getByPlaceholder('Comment…')).toHaveValue('');
			await reader.getByPlaceholder('Comment…').fill(body);
			wire.arm();
			await submitButton(reader).click();
			await expect.poll(() => wire.held(), { timeout: 20_000 }).not.toBeNull();
			expect(wire.held()!.request_id).not.toBe(closedAsk.request_id);
			await outsideClose(reader);
			// Change the actual tab before releasing the old target's queued server frames.
			await switchDocument(reader, other, false);
			wire.release();
			await expect(reader.locator('.cm-content')).toContainText(other.body.trim());
			await beginComment(reader, otherQuote);
			await expect(reader.getByPlaceholder('Comment…')).toHaveValue('');
			await reader.locator('.composer').getByRole('button', { name: 'Cancel', exact: true }).click();
			await switchDocument(reader, document);
			await beginComment(reader, quote);
			await expect(reader.getByPlaceholder('Comment…')).toHaveValue('');
			const freshBody = `Fresh after leaving ${stamp}`;
			await reader.getByPlaceholder('Comment…').fill(freshBody);
			await submitButton(reader).click();
			await expect(reader.getByPlaceholder('Comment…')).toHaveCount(0);
			expect(creates).toHaveLength(1); expect(otherCreates).toEqual([]);
			expectCorrelation(wire, creates[0], document.artifactId);
			await expectOneComment(request, document, [reader], quote, freshBody, 0, creates[0].client_id);
			expect(await storedComments(request, other)).toEqual([]);
		} finally { wire.release(); await context.close(); }
	});
}
