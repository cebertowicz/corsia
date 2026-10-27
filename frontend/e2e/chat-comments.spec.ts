import path from 'node:path';
import { expect, test, type Page } from './support/test';
import { chatComposer, createSession, openAppPastLanding, sidebarThread, TEST_TOKEN, waitForWsConnected } from './support/corsia';
import { injectMessage } from './support/mock';

const text = 'First selected phrase. Second selected phrase. Third selected phrase.';
const quotes = ['First selected phrase', 'Second selected phrase', 'Third selected phrase'];
const notes = ['First note', 'Second note', 'Third note'];
const frames = process.env.CHAT_COMMENT_FRAMES ?? path.resolve('test-results/chat-comments');

async function select(page: Page, id: string, quote: string) {
	await page.evaluate(({ id, quote }) => {
		const row = document.querySelector<HTMLElement>(`[data-message-id="${id}"]`)!;
		row.scrollIntoView({ block: 'center' });
		const body = row.querySelector<HTMLElement>('.long-message-body')!;
		const start = body.textContent!.indexOf(quote);
		const walker = document.createTreeWalker(body, NodeFilter.SHOW_TEXT);
		const range = document.createRange();
		let position = 0;
		let found = false;
		for (let node = walker.nextNode() as Text | null; node; node = walker.nextNode() as Text | null) {
			if (!found && start < position + node.length) { range.setStart(node, start - position); found = true; }
			if (found && start + quote.length <= position + node.length) { range.setEnd(node, start + quote.length - position); break; }
			position += node.length;
		}
		const selection = getSelection()!; selection.removeAllRanges(); selection.addRange(range);
	}, { id, quote });
	await expect(page.locator('.sel-comment')).toBeVisible();
}

for (const size of [1440, 393]) for (const scheme of ['light', 'dark'] as const) {
	test.describe(`chat comments ${size} ${scheme}`, () => {
		test.skip(() => test.info().project.name === 'chromium-mobile', 'viewport set by this spec');
		test.use({ seedPerTest: true, viewport: { width: size, height: size === 1440 ? 900 : 852 }, colorScheme: scheme, hasTouch: size === 393, isMobile: size === 393, deviceScaleFactor: 2 });
		test('stored long quotes and notes', async ({ page, request }) => {
			test.setTimeout(120_000);
			const title = `Stored ${size} ${scheme}`;
			const thread = await createSession(request, title);
			const quote = 'A long quoted sentence with source context. '.repeat(30);
			const note = 'A long note with a question about this comment. '.repeat(30);
			await injectMessage(request, thread, { role: 'user', content: 'A message', metadata: { reply_context: { comments: [{ id: 'c1', seq: 1, from_off: 0, to_off: quote.length, quote, anchor_prefix: '', anchor_suffix: '', note, cid: 'c1' }] } } }, TEST_TOKEN);
			await openAppPastLanding(page);
			if (size === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await sidebarThread(page, title).click();
			await waitForWsConnected(page);
			const block = page.locator('.sent-comment');
			await expect(block).toBeVisible();
			await expect(block.getByRole('button', { name: 'Show more' })).toBeVisible();
			const sizing = await block.evaluate(element => {
				const quote = element.querySelector<HTMLElement>('.quote-line')!;
				const note = element.querySelector<HTMLElement>('.sent-note')!;
				return { quoteFont: getComputedStyle(quote).fontSize, noteFont: getComputedStyle(note).fontSize, railHeight: quote.getBoundingClientRect().height, lineHeight: parseFloat(getComputedStyle(quote).lineHeight), quoteClipped: quote.scrollHeight > quote.clientHeight, noteClipped: note.scrollHeight > note.clientHeight };
			});
			expect(sizing.quoteFont).toBe(sizing.noteFont);
			expect(Math.abs(sizing.railHeight - sizing.lineHeight)).toBeLessThan(1);
			expect(sizing.quoteClipped).toBe(true);
			expect(sizing.noteClipped).toBe(true);
			const contrast = await block.evaluate(element => {
				const bubble = element.closest<HTMLElement>('.user-bubble')!;
				const rgb = (s: string) => (s.match(/[\d.]+/g) ?? []).slice(0, 3).map(Number);
				const luminance = (a: number[]) => a.map(v => { const n = v / 255; return n <= .04045 ? n / 12.92 : ((n + .055) / 1.055) ** 2.4; }).reduce((sum, v, i) => sum + v * [0.2126, 0.7152, 0.0722][i], 0);
				const bg = luminance(rgb(getComputedStyle(bubble).backgroundColor));
				return [...element.querySelectorAll<HTMLElement>('.quote.bubble, .sent-note, .sent-comment-toggle')].map(el => {
					let opacity = 1;
					for (let current: HTMLElement | null = el; current; current = current.parentElement) { opacity *= Number(getComputedStyle(current).opacity); if (current === bubble) break; }
					const background = rgb(getComputedStyle(bubble).backgroundColor);
					const foreground = rgb(getComputedStyle(el).color).map((value, i) => value * opacity + background[i] * (1 - opacity));
					const fg = luminance(foreground);
					return { ratio: (Math.max(fg, bg) + .05) / (Math.min(fg, bg) + .05), opacity };
				});
			});
			expect(contrast).toHaveLength(3);
			for (const result of contrast) { expect(result.ratio).toBeGreaterThanOrEqual(4.5); expect(result.opacity).toBe(1); }
			await page.screenshot({ path: path.join(frames, `long-collapsed-${size}-${scheme}.png`) });
			await block.getByRole('button', { name: 'Show more' }).click();
			await expect(block.getByRole('button', { name: 'Show less' })).toBeVisible();
			await expect.poll(() => block.locator('.quote-line').evaluate(el => el.scrollHeight <= el.clientHeight)).toBe(true);
			await expect.poll(() => block.locator('.sent-note').evaluate(el => el.scrollHeight <= el.clientHeight)).toBe(true);
			await block.locator('.sent-comment-source').hover();
			await expect(block.locator('.sent-comment-source')).toHaveCSS('background-color', 'rgba(0, 0, 0, 0)');
			await page.screenshot({ path: path.join(frames, `long-expanded-${size}-${scheme}.png`) });
		});
		test('select, edit and send', async ({ page, request }) => {
			test.setTimeout(120_000);
			const title = `Comments ${size} ${scheme}`;
			const thread = await createSession(request, title);
			const id = await injectMessage(request, thread, { role: 'assistant', content: text, metadata: { for_user: true } }, TEST_TOKEN);
			await openAppPastLanding(page);
			await page.addStyleTag({ content: '.version-overlay { display: none !important; }' });
			if (size === 393) await page.getByRole('button', { name: 'Toggle sidebar' }).click();
			await sidebarThread(page, title).click();
			await waitForWsConnected(page);
			await expect(page.locator(`[data-message-id="${id}"]`)).toBeVisible();
			for (let index = 0; index < quotes.length; index++) {
				await select(page, id, quotes[index]);
				await page.locator('.sel-comment').click();
				await page.getByPlaceholder('Comment…').fill(notes[index]);
				await page.getByPlaceholder('Comment…').press('Control+Enter');
				await expect(page.locator('.comment-chip')).toHaveCount(index + 1);
			}
			await page.locator('.comment-chip').first().click();
			await page.getByPlaceholder('Comment…').fill('Edited first note');
			await page.getByPlaceholder('Comment…').press('Control+Enter');
			await expect(page.locator('.comment-chip')).toHaveCount(3);
			await page.screenshot({ path: path.join(frames, `chips-${size}-${scheme}.png`) });
			await chatComposer(page).fill('Please address these comments');
			await chatComposer(page).press('Enter');
			await expect(page.locator('.sent-comment')).toHaveCount(3);
			await expect(page.getByText('Edited first note')).toBeVisible();
			await page.screenshot({ path: path.join(frames, `sent-${size}-${scheme}.png`) });
		});
	});
}
