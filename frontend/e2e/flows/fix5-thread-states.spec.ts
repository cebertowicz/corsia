import { test } from '../support/test';
import { expect } from '@playwright/test';
import { agentComment, reviewerSession, suggest } from '../support/liveConcurrency';
import { openLiveDocument, seedLiveDocument } from '../support/liveDocument';
import { setDebugVersionOverlay } from '../support/heldComments';
import { expectCaretLineVisible, expectDiscussionReachableByScroll, expectFieldAndSendUsable } from '../support/discussionVisible';

const quote = 'target line for a reply';
const text = Array.from({ length: 45 }, (_, i) => i === 25 ? quote : `document line ${i}`).join('\n\n');

/** One rule: a long reply never makes anything inside the card scroll. The field grows to its text, the card (or sheet) is the one box that scrolls, the field's bottom edge and Send stay in view as lines are typed, the highlight stays clear, and the whole thread is reachable by scrolling that box. */
for (const state of ['resolved', 'suggestion'] as const) test(`long reply keeps the field usable and the thread reachable on a ${state}`, async ({ page, request }, info) => {
	test.skip(info.project.name !== 'chromium-desktop');
	await page.setViewportSize({ width: 900, height: 420 });
	await setDebugVersionOverlay(request, false);
	const doc = await seedLiveDocument(request, `fix5-${state}`, text);
	const reviewer = await reviewerSession(request, `fix5-${state}`);
	const body = 'Existing thread. ' + 'Long original text. '.repeat(30);
	if (state === 'suggestion') await suggest(request, doc, reviewer.id, quote, 'replacement wording', body);
	else await agentComment(request, doc, reviewer.id, quote, body);
	await openLiveDocument(page, doc, 'document line 0');
	const scroller = page.locator('.cm-scroller');
	await scroller.evaluate((e) => { e.scrollTop = e.scrollHeight * 0.4; });
	await expect(page.locator('.cm-line').filter({ hasText: quote })).toBeVisible();
	await scroller.evaluate((e, sought) => {
		const target = [...document.querySelectorAll<HTMLElement>('.cm-line')].find((n) => n.textContent?.includes(sought))!;
		e.scrollTop += target.getBoundingClientRect().top - (e.getBoundingClientRect().bottom - 120);
	}, quote);
	const highlight = page.locator(state === 'suggestion' ? '.cm-suggestion-del' : '.cm-comment-anchor').filter({ hasText: quote });
	await highlight.click();
	let input = page.getByPlaceholder('Reply…');
	let card = input.locator('xpath=ancestor::*[contains(concat(" ", normalize-space(@class), " "), " ct ")][1]');
	await input.fill(Array.from({ length: 40 }, (_, i) => `first reply ${i}`).join('\n'));
	await card.getByRole('button', { name: 'Send reply' }).click();
	await expect(card.getByText('first reply 0')).toBeVisible();
	if (state === 'resolved') {
		await card.getByRole('button', { name: 'Resolve', exact: true }).click();
		await page.getByRole('button', { name: /^Resolved ·/ }).click();
		// Catch the opening while it is still animating: the field must never be clipped mid-transition.
		await page.evaluate(() => {
			const animate = Element.prototype.animate;
			Element.prototype.animate = function (...args: Parameters<Element['animate']>) {
				const animation = animate.apply(this, args);
				if (this.classList.contains('drawer-panel')) { animation.pause(); animation.currentTime = 10; }
				return animation;
			};
			document.addEventListener('animationstart', (event) => {
				const target = event.target;
				if (!(target instanceof HTMLElement) || !target.classList.contains('ct-res-body')) return;
				for (const animation of target.getAnimations()) { animation.pause(); animation.currentTime = 75; }
			}, { once: false });
		});
		const resolved = page.getByRole('region', { name: /Resolved comment by/ });
		await resolved.locator('.ct-res-head').click();
		input = resolved.getByPlaceholder('Reply…');
		card = resolved;
	}
	const second = Array.from({ length: 40 }, (_, i) => `second reply ${i}`);
	// A suggestion's reply scrolls with its card, so it is typed a line at a time: after each line the field is inside the popover's visible box and shows its last line, and Accept and Reject still take a tap.
	if (state === 'suggestion') for (let line = 1; line <= second.length; line++) {
		await input.fill(second.slice(0, line).join('\n'));
		await expectCaretLineVisible(card, input);
	} else await input.fill(second.join('\n'));
	await expectFieldAndSendUsable(card, input);
	if (state === 'resolved') {
		await expectCaretLineVisible(card, input);
		await expectDiscussionReachableByScroll(card, 'Existing thread');
		await expectDiscussionReachableByScroll(card, 'first reply 0');
		await page.evaluate(() => document.querySelector('.drawer-panel')?.getAnimations({ subtree: true }).forEach((animation) => animation.play()));
	} else {
		await expectDiscussionReachableByScroll(card, 'first reply 0');
		// The card may not sit on top of the text the comment is about.
		await expect.poll(() => highlight.evaluate((el) => {
			const rect = el.getBoundingClientRect();
			if (!rect.height || rect.top < 0 || rect.bottom > innerHeight) return false;
			const hit = document.elementFromPoint(rect.left + rect.width / 2, rect.top + rect.height / 2);
			return !!hit && (hit === el || el.contains(hit) || hit.contains(el));
		})).toBe(true);
	}
	await page.screenshot({ path: info.outputPath(`${state}.png`), animations: 'disabled' });
	await card.getByRole('button', { name: 'Send reply' }).click();
	await expect(card.getByText('second reply 0')).toBeVisible();
});
