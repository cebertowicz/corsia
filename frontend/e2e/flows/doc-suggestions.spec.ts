import fs from 'node:fs';
import { expect, test, type APIRequestContext, type Locator, type Page } from '@playwright/test';
import { saveEvidenceScreenshot } from '../support/corsia';
import { expectContrast, GRAPHIC, TEXT } from '../support/contrast';
import { openSuggestionDocument, setEvidenceViewport } from '../support/docSuggestions';

async function openSuggestionFlow(page: Page, request: APIRequestContext) {
	const text = '# Suggestions\n\nThe first line is loose.\n\nThe second line is fine.\n\nThe third line will change.\n';
	// Three suggestions left by another thread, the way an agent leaves them.
	const seeded = await openSuggestionDocument(page, request, text, [
		{ quote: 'is loose', replacement: 'is tight', body: 'Tighten the first line.' },
		{ quote: 'is fine', replacement: 'is wordy', body: 'Loosen the second line.' },
		{ quote: 'will change', replacement: 'stays put', body: 'Keep the third line.' },
	]);
	const [loose, fine, stale] = seeded.ids;
	await expect(seeded.editor).toContainText('The first line is loose.');
	const openComments = async () => {
		await page.getByRole('button', { name: /^3 open threads\b/ }).click();
		await expect(seeded.drawer).toBeVisible();
	};
	return { ...seeded, fine, loose, openComments, stale, text };
}

test('@flow:document-suggestions an agent\'s suggestions are refused when stale, replied to, accepted and rejected', async ({ page, request }, testInfo) => {
	test.setTimeout(90_000);
	await setEvidenceViewport(page, testInfo);
	const { artifactId, accept, docPath, drawer, editor, fine, loose, openComments, params, reject, reviewerId, reviewerTitle, row, stale, storedText, text } = await openSuggestionFlow(page, request);
	const refusedNote = 'The text this suggestion replaces has changed.';
	await openComments();
	for (const id of [loose, fine, stale]) await expect(accept(id)).toBeEnabled();
	await expect(drawer.getByRole('button', { name: `Accept all from ${reviewerTitle}`, exact: true })).toBeEnabled();

	// The file changes on disk underneath the open canvas, the way another program edits it. The open
	// document is live, so it neither refuses an HTTP save nor raises a banner: it takes the outside text
	// in at its next write. Until then the screen and the card still show the old text.
	await expect(page.getByText('Updated elsewhere since you opened it.')).toHaveCount(0);
	fs.writeFileSync(docPath, text.replace('will change', 'has moved on'));
	await expect.poll(storedText).toContain('The third line has moved on.');
	await expect(editor).toContainText('The third line will change.');
	// An open suggestion is drawn as strike and insert marks over its quote, not as a comment highlight.
	await expect(page.locator('.cm-suggestion-del').filter({ hasText: 'will change' })).toHaveCount(1);
	await expect(accept(stale)).toBeEnabled();
	const refusal = page.waitForResponse((response) => response.url().includes(`/api/comments/${stale}/decision`));
	await accept(stale).click();
	expect((await refusal).status()).toBe(409);
	// The card says why and stops offering an accept that cannot work.
	await expect(row(stale).getByRole('alert')).toContainText('no longer in the document');
	await expect(row(stale)).toContainText(refusedNote);
	await expect(accept(stale)).toBeDisabled();
	await expect(reject(stale)).toBeEnabled();
	await expect(row(stale).getByRole('button', { name: 'Show comment: text unavailable', exact: true })).toBeVisible();
	await expect(page.locator('.cm-suggestion-del').filter({ hasText: 'will change' })).toHaveCount(0);
	// The refused accept reaches the live document, which takes the file's text in: the screen now shows
	// it, and the refused card stays orphaned against it.
	await expect(editor).toContainText('The third line has moved on.');
	await expect(page.locator('.comment-status:has(.unplaced-dot)')).toBeVisible();
	await expect(page.getByText('Updated elsewhere since you opened it.')).toHaveCount(0);
	// The refused card's message is readable in both themes.
	await row(stale).getByRole('alert').scrollIntoViewIfNeeded();
	await expectContrast(page, testInfo, 'document-suggestions-refused', { refusal: { target: row(stale).getByRole('alert'), min: TEXT } });
	await expect(row(stale)).toContainText(refusedNote);
	await expect(accept(stale)).toBeDisabled();

	// A suggestion takes a reply like any comment. The drawer stayed open and scrolled down to the refused
	// card, so the reader scrolls back up to the first card to reply.
	await row(loose).scrollIntoViewIfNeeded();
	await row(loose).getByPlaceholder('Reply…').fill('Why tighter?');
	await row(loose).getByRole('button', { name: 'Send reply' }).click();
	await expect(row(loose)).toContainText('Why tighter?');
	const replyComposer = row(loose).getByPlaceholder('Reply…');
	await replyComposer.scrollIntoViewIfNeeded();
	await expect(replyComposer).toBeInViewport();
	await expect(row(loose).getByText('Why tighter?', { exact: true })).toBeInViewport();
	await saveEvidenceScreenshot(page, testInfo, 'document-suggestions-reply-composer', { fullPage: false });
	await expect.poll(async () => {
		const listed = await request.get(`/api/artifacts/${artifactId}/comments`, { params });
		expect(listed.ok()).toBeTruthy();
		return (await listed.json()).comments.find((comment: { id: string }) => comment.id === loose)?.replies;
	}).toEqual([expect.objectContaining({ body: 'Why tighter?' })]);

	await accept(loose).click();
	const accepted = row(loose).getByRole('region', { name: /^accepted suggestion by / });
	await expect(accepted).toBeVisible();
	await expect(editor).toContainText('The first line is tight.');
	await expect(accepted.locator('.ct-check')).toHaveCount(1);
	// The decided card keeps the discussion.
	await accepted.getByRole('button', { name: /Accepted by/ }).click();
	await expect(accepted).toContainText('Why tighter?');

	await reject(fine).click();
	const rejected = row(fine).getByRole('region', { name: /^rejected suggestion by / });
	await expect(rejected.getByRole('button', { name: /Rejected by/ })).toBeVisible();
	await expect(rejected.locator('.ct-cross')).toHaveCount(1);
	await expect(rejected.locator('.ct-check')).toHaveCount(0);
	await expect(editor).toContainText('The second line is fine.');
	// The decided cards' text and the rejected card's cross are readable in both themes.
	await rejected.locator('.ct-cross').scrollIntoViewIfNeeded();
	await expectContrast(page, testInfo, 'document-suggestions-decided', {
		rejectedText: { target: rejected.locator('.ct-res-text'), min: TEXT }, cross: { target: rejected.locator('.ct-cross'), min: GRAPHIC },
		acceptedText: { target: accepted.locator('.ct-res-text'), min: TEXT },
	});

	// A resolved comment's row shares the decided row's text style.
	const noted = await request.post('/api/delegation/docs/comments', {
		params, data: { artifact_id: artifactId, quote: 'second line', body: 'A note to resolve.', as_session_id: reviewerId },
	});
	expect(noted.ok(), await noted.text()).toBeTruthy();
	const note = (await noted.json()).comment.id as string;
	const resolvedNote = await request.patch(`/api/comments/${note}`, { params, data: { resolved: true } });
	expect(resolvedNote.ok(), await resolvedNote.text()).toBeTruthy();
	const resolvedText = row(note).locator('.ct-res-text');
	await expect(resolvedText).toHaveText('Resolved · 1 message');
	await resolvedText.scrollIntoViewIfNeeded();
	await expectContrast(page, testInfo, 'document-suggestions-resolved', { resolvedText: { target: resolvedText, min: TEXT } });
});

async function delayDecision(page: Page, commentId: string) {
	let release!: () => void;
	let requested!: () => void;
	const pending = new Promise<void>((resolve) => { release = resolve; });
	const arrived = new Promise<void>((resolve) => { requested = resolve; });
	await page.route(`**/api/comments/${commentId}/decision**`, async (route) => {
		requested();
		await pending;
		await route.continue();
	});
	return { arrived, release };
}

async function failOneDecision(page: Page, commentId: string, decision: 'accept' | 'reject') {
	let failed = false;
	await page.route(`**/api/comments/${commentId}/decision**`, async (route) => {
		if (!failed && route.request().postDataJSON().decision === decision) {
			failed = true;
			await route.fulfill({ status: 500, contentType: 'application/json', body: JSON.stringify({ detail: `Synthetic ${decision} failure` }) });
			return;
		}
		await route.continue();
	});
}

test('@flow:document-suggestions @surface:doc-line-start commented lines start where every line does', async ({ page, request }, testInfo) => {
	test.setTimeout(90_000);
	await setEvidenceViewport(page, testInfo);
	const text = '# Suggestions\n\nThe first line is loose.\n\nThe second line is fine.\n\nThe third line will change.\n';
	// Two plain comments, which highlight their anchor, and one open suggestion, which draws its own
	// strike and insert marks instead (it gets no comment highlight).
	const { editor } = await openSuggestionDocument(page, request, text, [
		{ quote: 'is loose', body: 'A note on the first line.' },
		{ quote: 'is fine', body: 'A note on the second line.' },
		{ quote: 'will change', replacement: 'stays put', body: 'Keep the third line.' },
	]);
	await expect(page.locator('.cm-comment-anchor')).toHaveCount(2);
	await expect(page.locator('.cm-suggestion-del')).toHaveCount(1);
	// Each line's first text glyph, skipping widgets such as the comment marker, against the
	// line's own content box and the scroller's visible left edge.
	const starts = await editor.evaluate((content) => {
		const scrollerLeft = content.closest('.cm-scroller')!.getBoundingClientRect().left;
		return [...content.querySelectorAll<HTMLElement>('.cm-line')].filter((line) => line.textContent?.trim()).map((line) => {
			const walker = document.createTreeWalker(line, NodeFilter.SHOW_TEXT, {
				acceptNode: (node) => (node.parentElement?.closest('[contenteditable="false"]') || !node.textContent?.trim()
					? NodeFilter.FILTER_REJECT : NodeFilter.FILTER_ACCEPT),
			});
			const first = walker.nextNode()!;
			const glyph = document.createRange();
			glyph.setStart(first, 0);
			glyph.setEnd(first, 1);
			const box = line.getBoundingClientRect();
			return {
				text: line.innerText.trim().slice(0, 24),
				commented: !!line.querySelector('.cm-comment-anchor, .cm-suggestion-del'),
				glyphLeft: glyph.getBoundingClientRect().left,
				contentLeft: box.left + Number.parseFloat(getComputedStyle(line).paddingLeft),
				scrollerLeft,
			};
		});
	});
	expect(starts.filter((line) => line.commented)).toHaveLength(3);
	for (const line of starts) {
		expect.soft(line.glyphLeft, `"${line.text}" first glyph starts at its line's content edge`).toBeCloseTo(line.contentLeft, 0);
		expect.soft(line.glyphLeft, `"${line.text}" first glyph is inside the visible editor`).toBeGreaterThanOrEqual(line.scrollerLeft);
	}
	await saveEvidenceScreenshot(page, testInfo, 'document-suggestions-line-start', { fullPage: false });
});

test('@flow:document-suggestions keyboard reaches Accept then Reject with a visible focus ring, and Enter decides', async ({ page, request }, testInfo) => {
	test.setTimeout(90_000);
	await setEvidenceViewport(page, testInfo);
	const { accept, openComments, reject, row, loose } = await openSuggestionFlow(page, request);
	await openComments();
	// Tab in from whatever control precedes Accept, so focus arrives the way a keyboard user's does.
	const tabIntoAccept = async () => {
		await accept(loose).focus();
		await page.keyboard.press('Shift+Tab');
		await expect(accept(loose)).not.toBeFocused();
		await page.keyboard.press('Tab');
	};
	const ring = async (target: Locator) => {
		await expect(target).toBeFocused();
		const style = await target.evaluate((element) => {
			const own = getComputedStyle(element);
			return { outline: own.outlineStyle, width: Number.parseFloat(own.outlineWidth), colour: own.outlineColor, shadow: own.boxShadow };
		});
		console.log(`focus ${testInfo.project.name} ${page.viewportSize()?.width}px ${await target.innerText()}: outline ${style.outline} ${style.width}px ${style.colour}; shadow ${style.shadow}`);
		// The card's own ring, not the browser default, which can vanish against the accent fill.
		expect(style.outline === 'solid' && style.width >= 2, `${await target.innerText()} shows the card's focus ring`).toBe(true);
	};
	for (const theme of ['light', 'dark'] as const) {
		await page.emulateMedia({ colorScheme: theme });
		await expect(page.locator('html')).toHaveAttribute('data-theme', theme);
		await tabIntoAccept();
		await ring(accept(loose));
		await saveEvidenceScreenshot(page, testInfo, `document-suggestions-keyboard-accept-${theme}`, { fullPage: false });
		await page.keyboard.press('Tab');
		await ring(reject(loose));
		await saveEvidenceScreenshot(page, testInfo, `document-suggestions-keyboard-reject-${theme}`, { fullPage: false });
	}
	await page.emulateMedia({ colorScheme: 'light' });
	// A solid 2px ring still fails a keyboard user if its colour fades into the card, so measure it in both themes.
	await tabIntoAccept();
	await expectContrast(page, testInfo, 'document-suggestions-focus-ring-accept', { acceptRing: { target: accept(loose), min: GRAPHIC, paint: 'outline' } });
	await page.keyboard.press('Tab');
	await expectContrast(page, testInfo, 'document-suggestions-focus-ring-reject', { rejectRing: { target: reject(loose), min: GRAPHIC, paint: 'outline' } });
	await page.keyboard.press('Shift+Tab');
	await expect(accept(loose)).toBeFocused();
	await page.keyboard.press('Enter');
	await expect(row(loose).getByRole('region', { name: /^accepted suggestion by / })).toBeVisible();
});

test('@flow:document-suggestions accept-all stays singular while its real request is busy', async ({ page, request }, testInfo) => {
	test.setTimeout(90_000);
	await setEvidenceViewport(page, testInfo);
	const { accept, drawer, loose, openComments, reviewerTitle, row } = await openSuggestionFlow(page, request);
	await openComments();
	const acceptAll = drawer.getByRole('button', { name: new RegExp(`^(Accept all from ${reviewerTitle}|Accepting…)$`) });
	const delayed = await delayDecision(page, loose);
	await acceptAll.click();
	await delayed.arrived;
	await expect(acceptAll).toHaveText('Accepting…');
	await expect(acceptAll).toHaveAttribute('aria-busy', 'true');
	await expect(drawer.locator('.suggestion-status button')).toBeDisabled();
	await expectContrast(page, testInfo, 'document-suggestions-accept-all-busy', { accepting: { target: acceptAll, min: TEXT } });
	delayed.release();
	await expect(row(loose).getByRole('region', { name: /^accepted suggestion by / })).toBeVisible();
	await expect(accept(loose)).toHaveCount(0);
});

test('@flow:document-suggestions suppresses duplicate card decisions and scopes pending labels', async ({ page, request }, testInfo) => {
	test.setTimeout(90_000);
	await setEvidenceViewport(page, testInfo);
	const { accept, fine, loose, openComments, reject, row } = await openSuggestionFlow(page, request);
	await openComments();
	let looseDecisionRequests = 0;
	let fineDecisionRequests = 0;
	page.on('request', (outgoing) => {
		if (outgoing.url().includes(`/api/comments/${loose}/decision`)) looseDecisionRequests += 1;
		if (outgoing.url().includes(`/api/comments/${fine}/decision`)) fineDecisionRequests += 1;
	});
	const delayedAccept = await delayDecision(page, loose);
	const accepting = row(loose).getByRole('button', { name: /^(Accept|Accepting…)$/ });
	await accept(loose).dblclick();
	await delayedAccept.arrived;
	await expect(accepting).toHaveText('Accepting…');
	await expect(accepting).toHaveAttribute('aria-busy', 'true');
	await expect(reject(loose)).toBeDisabled();
	await expect(accept(fine)).toBeEnabled();
	expect(looseDecisionRequests).toBe(1);
	await expectContrast(page, testInfo, 'document-suggestions-accept-pending', { accepting: { target: accepting, min: TEXT } });
	delayedAccept.release();
	await expect(row(loose).getByRole('region', { name: /^accepted suggestion by / })).toBeVisible();
	expect(looseDecisionRequests).toBe(1);
	const delayedReject = await delayDecision(page, fine);
	const rejecting = row(fine).getByRole('button', { name: /^(Reject|Rejecting…)$/ });
	await reject(fine).dblclick();
	await delayedReject.arrived;
	await expect(rejecting).toHaveText('Rejecting…');
	await expect(rejecting).toHaveAttribute('aria-busy', 'true');
	await expect(accept(fine)).toBeDisabled();
	expect(fineDecisionRequests).toBe(1);
	await expectContrast(page, testInfo, 'document-suggestions-reject-pending', { rejecting: { target: rejecting, min: TEXT } });
	delayedReject.release();
	await expect(row(fine).getByRole('region', { name: /^rejected suggestion by / })).toBeVisible();
	expect(fineDecisionRequests).toBe(1);
});

test('@flow:document-suggestions recovers from inline decision and accept-all failures', async ({ page, request }, testInfo) => {
	test.setTimeout(90_000);
	await setEvidenceViewport(page, testInfo);
	const first = await openSuggestionFlow(page, request);
	await first.openComments();
	await failOneDecision(page, first.loose, 'accept');
	await first.accept(first.loose).click();
	await expect(first.row(first.loose).getByRole('alert')).toHaveText('Synthetic accept failure');
	await expect(first.accept(first.fine)).toBeEnabled();
	await expect(first.row(first.loose).getByRole('region', { name: /^accepted suggestion by / })).toHaveCount(0);
	await expectContrast(page, testInfo, 'document-suggestions-accept-failure', { failure: { target: first.row(first.loose).getByRole('alert'), min: TEXT } });
	await first.accept(first.loose).click();
	await expect(first.row(first.loose).getByRole('region', { name: /^accepted suggestion by / })).toBeVisible();
	await expect(first.row(first.loose).getByRole('alert')).toHaveCount(0);
	await failOneDecision(page, first.fine, 'reject');
	await first.reject(first.fine).click();
	await expect(first.row(first.fine).getByRole('alert')).toHaveText('Synthetic reject failure');
	await expect(first.reject(first.fine)).toBeEnabled();
	await first.reject(first.fine).click();
	await expect(first.row(first.fine).getByRole('region', { name: /^rejected suggestion by / })).toBeVisible();
	const second = await openSuggestionFlow(page, request);
	await second.openComments();
	const acceptAll = second.drawer.getByRole('button', { name: `Accept all from ${second.reviewerTitle}`, exact: true });
	await failOneDecision(page, second.loose, 'accept');
	await acceptAll.click();
	await expect(second.drawer.getByRole('alert')).toHaveText('Synthetic accept failure');
	await expect(acceptAll).toHaveText(`Accept all from ${second.reviewerTitle}`);
	await expect(second.drawer.getByText(/skipped$/)).toHaveCount(0);
	await expectContrast(page, testInfo, 'document-suggestions-accept-all-failure', { failure: { target: second.drawer.getByRole('alert'), min: TEXT } });
	await acceptAll.click();
	await expect(second.row(second.loose).getByRole('region', { name: /^accepted suggestion by / })).toBeVisible();
});
