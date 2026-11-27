import { test } from '../support/test';
import { expect, type Page } from '@playwright/test';
import { openLivePair } from '../support/liveConcurrency';
import { seedLiveDocument, serverText, typeAtDocumentEnd } from '../support/liveDocument';
import {
	beginComment, commentWire, expectCorrelation, expectOneComment, observeCreates, submitButton, type CommentWire,
} from '../support/liveCommentProof';

const width = 1280, height = 960;
const unanswered = 'Couldn’t attach your comment. Your draft is kept. Try again.';
const changed = 'The document changed before your comment could be attached. Your draft is kept. Try again.';
/** commentSelection.ts RESERVE_BUDGET_MS: how long a capture keeps asking on a document that keeps moving. */
const BUDGET_MS = 3_000;

function failure(page: Page, text: string) { return page.getByRole('status').filter({ hasText: text }); }

// Real editor and server throughout; the socket tap only delays the server's real answer, byte for byte.
test('@surface:live-document @comment-proof a reserved answer that comes after the 3 s budget still attaches the comment once', async ({ browser, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'One desktop layout: this proves timing, not layout.');
	test.setTimeout(120_000);
	const stamp = `${Date.now()}`, quote = `slow answer ${stamp}`, body = `Comment on a slow server ${stamp}`;
	const document = await seedLiveDocument(request, `slow-answer-${stamp}`, `lead ${stamp}\n${quote}\ntail ${stamp}\n`);
	let wire!: CommentWire;
	const pair = await openLivePair(browser, document, width, height, async context => { wire = await commentWire(context); });
	try {
		const { reader, writer } = pair, creates = observeCreates(reader, document.artifactId);
		await beginComment(reader, quote, body);
		wire.arm();
		await submitButton(reader).click();
		await expect.poll(() => wire.held(), { timeout: 20_000 }).not.toBeNull();
		expect(wire.held()!.reserved, 'the held answer is the server reserving the selection').toBe(true);
		// Held past the 3 s budget but well inside the answer's own allowance.
		await reader.waitForTimeout(4_000);
		expect(creates, 'nothing posts while the answer is held').toEqual([]);
		wire.release();
		await expect.poll(() => creates.length, { timeout: 15_000, message: 'the late reserved answer leads to one POST' }).toBe(1);
		await expect(reader.getByPlaceholder('Comment, or @ to tag an agent')).toHaveCount(0);
		await expect(failure(reader, unanswered)).toHaveCount(0);
		await expect(failure(reader, changed)).toHaveCount(0);
		const file = await serverText(request, document);
		expectCorrelation(wire, creates[0], document.artifactId);
		await expectOneComment(request, document, [reader, writer], quote, body, file.indexOf(quote), creates[0].client_id);
		expect(creates).toHaveLength(1);
	} finally { wire?.release(); await pair.close(); }
});

test('@surface:live-document @comment-proof on a document that keeps moving, the capture still ends within the budget plus one answer', async ({ browser, request }, testInfo) => {
	test.skip(testInfo.project.name !== 'chromium-desktop', 'One desktop layout: this proves timing, not layout.');
	test.setTimeout(120_000);
	const stamp = `${Date.now()}`, quote = `moving target ${stamp}`, body = `Comment on a moving document ${stamp}`;
	const document = await seedLiveDocument(request, `moving-${stamp}`, `lead ${stamp}\n${quote}\ntail ${stamp}\n`);
	let wire!: CommentWire;
	const pair = await openLivePair(browser, document, width, height, async context => { wire = await commentWire(context); });
	let typing = true, edits = 0;
	try {
		const { reader, writer } = pair, creates = observeCreates(reader, document.artifactId);
		await beginComment(reader, quote, body);
		await typeAtDocumentEnd(writer, ' ');
		// A remote edit every 40 ms, away from the selection, from before the press until the capture ends.
		const typist = (async () => {
			while (typing) { await writer.keyboard.insertText('m'); edits++; await writer.waitForTimeout(40); }
		})();
		await expect.poll(() => edits, { timeout: 10_000 }).toBeGreaterThan(5);
		const pressed = Date.now(), asksBefore = wire.asks.length;
		await submitButton(reader).click();
		// The capture ends one of two ways: it attached (one POST) or it said the document changed.
		await expect.poll(async () => creates.length > 0 || await failure(reader, changed).count() > 0,
			{ timeout: 20_000, intervals: [50] }).toBe(true);
		const ended = Date.now() - pressed;
		typing = false; await typist;
		const asks = wire.asks.slice(asksBefore).filter(ask => ask.artifact_id === document.artifactId && ask.reserve);
		const answered = wire.answers.filter(answer => asks.some(ask => ask.request_id === answer.request_id));
		testInfo.annotations.push({ type: 'capture', description:
			`ended=${creates.length ? 'posted' : 'changed'} after ${ended} ms; asks=${asks.length}; answers=${answered.length}; remote edits=${edits}` });
		await expect(failure(reader, unanswered)).toHaveCount(0);
		// One answer on a local server comes back in well under a second; the 10 s answer allowance must never be reached.
		expect(ended, 'the budget, not the answer allowance, bounds a moving document').toBeLessThan(BUDGET_MS + 2_000);
		expect(creates.length).toBeLessThanOrEqual(1);
		if (creates.length) expectCorrelation(wire, creates[0], document.artifactId);
		else await expect(reader.getByPlaceholder('Comment, or @ to tag an agent')).toHaveValue(body);
	} finally { typing = false; wire?.release(); await pair.close(); }
});
