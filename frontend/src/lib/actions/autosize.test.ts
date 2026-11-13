import { describe, expect, it } from 'vitest';
import { autosize } from './autosize';

function field(scrollHeight: () => number, maxHeight = '100px') {
	const node = document.createElement('textarea');
	document.body.append(node);
	Object.defineProperty(node, 'scrollHeight', { get: scrollHeight, configurable: true });
	node.style.maxHeight = maxHeight;
	node.style.boxSizing = 'border-box';
	return node;
}

describe('autosize', () => {
	it('sizes the field to its text', () => {
		const node = field(() => 54);
		autosize(node, 'a long reply that wraps');
		expect(node.style.height).toBe('54px');
		expect(node.style.overflowY).toBe('hidden');
	});

	it('caps the height and scrolls past it', () => {
		const node = field(() => 240);
		autosize(node, 'many lines');
		expect(node.style.height).toBe('100px');
		expect(node.style.overflowY).toBe('auto');
	});

	it('treats a field holding exactly its cap of text as fitting, though the cap is fractional', () => {
		// Five lines read scrollHeight 126 against a computed cap of 125.953px: a subpixel over must not make it scrollable.
		const node = field(() => 126, '125.953px');
		autosize(node, 'five lines');
		expect(node.style.overflowY).toBe('hidden');
		expect(node.style.height).toBe('125.953px');
		const sixth = field(() => 148, '125.953px');
		autosize(sixth, 'six lines');
		expect(sixth.style.overflowY).toBe('auto');
	});

	it('keeps the text at the top while the height eases below the cap, and scrolls freely past it', () => {
		const small = field(() => 54);
		const action = autosize(small, 'a line');
		small.scrollTop = 18; small.dispatchEvent(new Event('scroll'));
		expect(small.scrollTop).toBe(0);
		action.destroy?.();
		const capped = field(() => 240);
		autosize(capped, 'many lines');
		capped.scrollTop = 18; capped.dispatchEvent(new Event('scroll'));
		expect(capped.scrollTop).toBe(18);
	});

	it('with fit and no cap grows until its scroller is full, in whole lines, then scrolls', () => {
		const owner = document.createElement('div');
		owner.style.overflowY = 'auto';
		document.body.append(owner);
		const node = field(() => 640, 'none');
		node.style.lineHeight = '20px';
		node.style.borderTopWidth = node.style.borderBottomWidth = '5px';
		owner.append(node);
		// The scroller shows 300px and holds 120px besides the field, so at full height (650px) it overflows by 470px.
		Object.defineProperty(owner, 'clientHeight', { get: () => 300, configurable: true });
		Object.defineProperty(owner, 'scrollHeight', { get: () => 120 + (Number.parseFloat(node.style.height) || 0), configurable: true });
		autosize(node, { value: 'a pasted page', fit: true });
		// 650 - 470 = 180px, less 10px of border, holds 8 whole 20px lines: 160px of text plus the border.
		expect(node.style.height).toBe('170px');
		expect(node.style.overflowY).toBe('auto');
	});

	it('with fit never drops below one line, however full its scroller already is', () => {
		const owner = document.createElement('div');
		owner.style.overflowY = 'auto';
		document.body.append(owner);
		const node = field(() => 640, 'none');
		node.style.lineHeight = '20px';
		owner.append(node);
		Object.defineProperty(owner, 'clientHeight', { get: () => 300, configurable: true });
		Object.defineProperty(owner, 'scrollHeight', { get: () => 2000, configurable: true });
		autosize(node, { value: 'a pasted page', fit: true });
		expect(node.style.height).toBe('20px');
	});

	it('lets a sticky reply grow despite discussion overflowing behind it', () => {
		const owner = document.createElement('div');
		owner.style.overflowY = 'auto'; owner.style.padding = '10px';
		document.body.append(owner);
		const pinned = document.createElement('div');
		pinned.setAttribute('data-autosize-sticky', ''); pinned.style.position = 'sticky';
		owner.append(pinned);
		const node = field(() => 640, 'none'); node.style.lineHeight = '20px'; pinned.append(node);
		Object.defineProperty(owner, 'clientHeight', { get: () => 300, configurable: true });
		Object.defineProperty(owner, 'scrollHeight', { get: () => 2000, configurable: true });
		Object.defineProperty(pinned, 'offsetHeight', { get: () => 20 + (Number.parseFloat(node.style.height) || 0), configurable: true });
		autosize(node, { value: 'a pasted page', fit: true });
		expect(node.style.height).toBe('260px');
		expect(node.style.overflowY).toBe('auto');
	});

	it('reserves the thread before fitting its sticky reply', () => {
		const owner = document.createElement('div');
		owner.style.overflowY = 'auto'; document.body.append(owner);
		const thread = document.createElement('div'); thread.setAttribute('data-autosize-room', ''); owner.append(thread);
		const pinned = document.createElement('div'); pinned.setAttribute('data-autosize-sticky', ''); pinned.style.position = 'sticky'; thread.append(pinned);
		const node = field(() => 640, 'none'); node.style.lineHeight = '20px'; pinned.append(node);
		Object.defineProperty(owner, 'clientHeight', { get: () => 300, configurable: true });
		Object.defineProperty(thread, 'offsetHeight', { get: () => 120 + (Number.parseFloat(node.style.height) || 0), configurable: true });
		Object.defineProperty(pinned, 'offsetHeight', { get: () => 20 + (Number.parseFloat(node.style.height) || 0), configurable: true });
		autosize(node, { value: 'a pasted page', fit: true });
		expect(node.style.height).toBe('180px');
		expect(node.style.overflowY).toBe('auto');
	});

	it('keeps two lines for a reply when the thread fills its view', () => {
		const owner = document.createElement('div'); owner.style.overflowY = 'auto'; document.body.append(owner);
		const thread = document.createElement('div'); thread.setAttribute('data-autosize-room', ''); owner.append(thread);
		const pinned = document.createElement('div'); pinned.setAttribute('data-autosize-sticky', ''); pinned.style.position = 'sticky'; thread.append(pinned);
		const node = field(() => 640, 'none'); node.style.lineHeight = '20px'; pinned.append(node);
		Object.defineProperty(owner, 'clientHeight', { get: () => 300, configurable: true });
		Object.defineProperty(thread, 'offsetHeight', { get: () => 400 + (Number.parseFloat(node.style.height) || 0), configurable: true });
		autosize(node, { value: 'a pasted page', fit: true });
		expect(node.style.height).toBe('40px');
	});

	it('makes bounded discussion keyboard-scrollable and releases it when the reply field leaves', () => {
		const owner = document.createElement('div'); owner.style.overflowY = 'auto'; document.body.append(owner);
		const thread = document.createElement('div'); thread.setAttribute('data-autosize-room', ''); owner.append(thread);
		const original = document.createElement('div'); original.setAttribute('data-discussion-original', '');
		const originalText = document.createElement('div'); originalText.className = 'ct-markdown'; originalText.style.lineHeight = '20px'; original.append(originalText);
		const replies = document.createElement('div'); replies.setAttribute('data-discussion-replies', '');
		const reply = document.createElement('div'); reply.className = 'ct-reply';
		const replyText = document.createElement('div'); replyText.className = 'ct-markdown'; replyText.style.lineHeight = '20px'; reply.append(replyText); replies.append(reply);
		const pinned = document.createElement('div'); pinned.setAttribute('data-autosize-sticky', ''); pinned.style.position = 'sticky';
		thread.append(original, replies, pinned);
		const node = field(() => 640, 'none'); node.style.lineHeight = '20px'; pinned.append(node);
		Object.defineProperty(owner, 'clientHeight', { get: () => 200 });
		for (const region of [original, replies]) Object.defineProperty(region, 'offsetHeight', { get: () => Number.parseFloat(region.style.maxHeight) || 80 });
		Object.defineProperty(thread, 'offsetHeight', { get: () => 80 + original.offsetHeight + replies.offsetHeight + (Number.parseFloat(node.style.height) || 0) });
		const action = autosize(node, { value: 'a long reply', fit: true });
		expect(original.getAttribute('tabindex')).toBe('0');
		expect(replies.getAttribute('tabindex')).toBe('0');
		action.destroy();
		expect(original.style.maxHeight).toBe('');
		expect(replies.style.maxHeight).toBe('');
		expect(original.hasAttribute('tabindex')).toBe(false);
		expect(replies.hasAttribute('tabindex')).toBe(false);
	});

	it('caps the comment text at whole lines so no half line sits against the replies', () => {
		const owner = document.createElement('div'); owner.style.overflowY = 'auto'; document.body.append(owner);
		const thread = document.createElement('div'); thread.setAttribute('data-autosize-room', ''); owner.append(thread);
		const original = document.createElement('div'); original.setAttribute('data-discussion-original', '');
		const originalText = document.createElement('div'); originalText.className = 'ct-markdown'; originalText.style.lineHeight = '20px'; original.append(originalText);
		const replies = document.createElement('div'); replies.setAttribute('data-discussion-replies', '');
		const reply = document.createElement('div'); reply.className = 'ct-reply';
		const replyText = document.createElement('div'); replyText.className = 'ct-markdown'; replyText.style.lineHeight = '20px'; reply.append(replyText); replies.append(reply);
		const pinned = document.createElement('div'); pinned.setAttribute('data-autosize-sticky', ''); pinned.style.position = 'sticky';
		thread.append(original, replies, pinned);
		const node = field(() => 640, 'none'); node.style.lineHeight = '20px'; pinned.append(node);
		// 253px of room is not a multiple of the 20px line, so half the leftover is 52.5px.
		Object.defineProperty(owner, 'clientHeight', { get: () => 253 });
		Object.defineProperty(original, 'offsetHeight', { get: () => Number.parseFloat(original.style.maxHeight) || 400 });
		Object.defineProperty(replies, 'offsetHeight', { get: () => Number.parseFloat(replies.style.maxHeight) || 400 });
		Object.defineProperty(thread, 'offsetHeight', { get: () => 80 + original.offsetHeight + replies.offsetHeight + (Number.parseFloat(node.style.height) || 0) });
		autosize(node, { value: 'a long reply', fit: true });
		const originalRoom = Number.parseFloat(original.style.maxHeight);
		expect(originalRoom).toBeGreaterThan(0);
		expect(originalRoom % 20).toBe(0);
		expect(Number.parseFloat(replies.style.maxHeight)).toBeGreaterThanOrEqual(20);
	});

	it('ends the comment text between lines when a paragraph gap is not a multiple of the line height', () => {
		const owner = document.createElement('div'); owner.style.overflowY = 'auto'; document.body.append(owner);
		const thread = document.createElement('div'); thread.setAttribute('data-autosize-room', ''); owner.append(thread);
		const original = document.createElement('div'); original.setAttribute('data-discussion-original', '');
		const originalText = document.createElement('div'); originalText.className = 'ct-markdown'; originalText.style.lineHeight = '21px'; originalText.append('A first line.', 'A second paragraph, line one.', 'and line two.'); original.append(originalText);
		const replies = document.createElement('div'); replies.setAttribute('data-discussion-replies', '');
		const reply = document.createElement('div'); reply.className = 'ct-reply';
		const replyText = document.createElement('div'); replyText.className = 'ct-markdown'; replyText.style.lineHeight = '21px'; reply.append(replyText); replies.append(reply);
		const pinned = document.createElement('div'); pinned.setAttribute('data-autosize-sticky', ''); pinned.style.position = 'sticky';
		thread.append(original, replies, pinned);
		const node = field(() => 640, 'none'); node.style.lineHeight = '21px'; pinned.append(node);
		// Layout in a real browser: one line, a 16px gap, then a paragraph of two lines, so the lines end at 21, 58 and 79 and 42 falls inside the second paragraph's first line.
		const rects = [{ top: 0, bottom: 21, height: 21 }, { top: 37, bottom: 58, height: 21 }, { top: 58, bottom: 79, height: 21 }];
		let index = 0;
		const proto = Range.prototype as unknown as { getClientRects?: () => unknown[] };
		proto.getClientRects = () => [rects[index++ % rects.length]];
		try {
			Object.defineProperty(owner, 'clientHeight', { get: () => 253 });
			Object.defineProperty(original, 'offsetHeight', { get: () => Number.parseFloat(original.style.maxHeight) || 400 });
			Object.defineProperty(replies, 'offsetHeight', { get: () => Number.parseFloat(replies.style.maxHeight) || 400 });
			Object.defineProperty(thread, 'offsetHeight', { get: () => 80 + original.offsetHeight + replies.offsetHeight + (Number.parseFloat(node.style.height) || 0) });
			autosize(node, { value: 'a long reply', fit: true });
			expect([21, 58, 79]).toContain(Number.parseFloat(original.style.maxHeight));
		} finally {
			delete proto.getClientRects;
		}
	});

	it('leaves the replies the newest reply whole before the comment takes another line', () => {
		const owner = document.createElement('div'); owner.style.overflowY = 'auto'; document.body.append(owner);
		const thread = document.createElement('div'); thread.setAttribute('data-autosize-room', ''); owner.append(thread);
		const original = document.createElement('div'); original.setAttribute('data-discussion-original', '');
		const originalText = document.createElement('div'); originalText.className = 'ct-markdown'; originalText.style.lineHeight = '21px'; originalText.append('A comment of six lines.'); original.append(originalText);
		const replies = document.createElement('div'); replies.setAttribute('data-discussion-replies', '');
		const reply = document.createElement('div'); reply.className = 'ct-reply';
		const replyText = document.createElement('div'); replyText.className = 'ct-markdown'; replyText.style.lineHeight = '21px'; replyText.append('A newest reply of two lines.'); reply.append(replyText); replies.append(reply);
		const pinned = document.createElement('div'); pinned.setAttribute('data-autosize-sticky', ''); pinned.style.position = 'sticky';
		thread.append(original, replies, pinned);
		const node = field(() => 640, 'none'); node.style.lineHeight = '21px'; pinned.append(node);
		// The newest reply is a 21px header and two 21px lines, 63px whole; the comment's six lines end at 21, 42, 63, 84, 105 and 126.
		reply.getBoundingClientRect = () => ({ top: 0, bottom: 63, height: 63 }) as DOMRect;
		replyText.getBoundingClientRect = () => ({ top: 21, bottom: 63, height: 42 }) as DOMRect;
		const proto = Range.prototype as unknown as { getClientRects?: (this: Range) => unknown[] };
		proto.getClientRects = function (this: Range) { return this.startContainer.textContent === 'A comment of six lines.' ? [1, 2, 3, 4, 5, 6].map(n => ({ top: (n - 1) * 21, bottom: n * 21, height: 21 })) : []; };
		try {
			// 222px of view less 80px of chrome and a two-line field leaves 100px: half of it would be 50px, but the reply needs 63.
			Object.defineProperty(owner, 'clientHeight', { get: () => 222 });
			Object.defineProperty(original, 'offsetHeight', { get: () => Number.parseFloat(original.style.maxHeight) || 126 });
			Object.defineProperty(replies, 'offsetHeight', { get: () => Number.parseFloat(replies.style.maxHeight) || 200 });
			Object.defineProperty(thread, 'offsetHeight', { get: () => 80 + original.offsetHeight + replies.offsetHeight + (Number.parseFloat(node.style.height) || 0) });
			autosize(node, { value: 'a long reply', fit: true });
			expect(Number.parseFloat(replies.style.maxHeight)).toBeGreaterThanOrEqual(63);
			expect(Number.parseFloat(original.style.maxHeight)).toBe(21);
		} finally {
			delete proto.getClientRects;
		}
	});

	it('ends a newest reply too tall for what is left on one of its own line ends', () => {
		const owner = document.createElement('div'); owner.style.overflowY = 'auto'; document.body.append(owner);
		const thread = document.createElement('div'); thread.setAttribute('data-autosize-room', ''); owner.append(thread);
		const original = document.createElement('div'); original.setAttribute('data-discussion-original', '');
		const originalText = document.createElement('div'); originalText.className = 'ct-markdown'; originalText.style.lineHeight = '21px'; originalText.append('A comment of six lines.'); original.append(originalText);
		const replies = document.createElement('div'); replies.setAttribute('data-discussion-replies', '');
		const reply = document.createElement('div'); reply.className = 'ct-reply';
		const replyText = document.createElement('div'); replyText.className = 'ct-markdown'; replyText.style.lineHeight = '21px'; replyText.append('A newest reply of four lines.'); reply.append(replyText); replies.append(reply);
		const pinned = document.createElement('div'); pinned.setAttribute('data-autosize-sticky', ''); pinned.style.position = 'sticky';
		thread.append(original, replies, pinned);
		const node = field(() => 640, 'none'); node.style.lineHeight = '21px'; pinned.append(node);
		// A 21px header and four 21px lines: 105px whole, ending at 21, 42, 63, 84 and 105.
		reply.getBoundingClientRect = () => ({ top: 0, bottom: 105, height: 105 }) as DOMRect;
		replyText.getBoundingClientRect = () => ({ top: 21, bottom: 105, height: 84 }) as DOMRect;
		const proto = Range.prototype as unknown as { getClientRects?: (this: Range) => unknown[] };
		const lines = (count: number) => Array.from({ length: count }, (_, n) => ({ top: n * 21, bottom: (n + 1) * 21, height: 21 }));
		proto.getClientRects = function (this: Range) { return this.startContainer.textContent === 'A comment of six lines.' ? lines(6) : lines(5); };
		try {
			// 100px is left: the comment keeps its first line (21px), so the reply has 79px, which falls inside its fourth line.
			Object.defineProperty(owner, 'clientHeight', { get: () => 222 });
			Object.defineProperty(original, 'offsetHeight', { get: () => Number.parseFloat(original.style.maxHeight) || 126 });
			Object.defineProperty(replies, 'offsetHeight', { get: () => Number.parseFloat(replies.style.maxHeight) || 200 });
			Object.defineProperty(thread, 'offsetHeight', { get: () => 80 + original.offsetHeight + replies.offsetHeight + (Number.parseFloat(node.style.height) || 0) });
			autosize(node, { value: 'a long reply', fit: true });
			expect(Number.parseFloat(original.style.maxHeight)).toBe(21);
			expect(Number.parseFloat(replies.style.maxHeight)).toBe(63);
		} finally {
			delete proto.getClientRects;
		}
	});

	it('keeps a long original readable while the first reply grows', () => {
		const owner = document.createElement('div'); owner.style.overflowY = 'auto'; document.body.append(owner);
		const thread = document.createElement('div'); thread.setAttribute('data-autosize-room', ''); owner.append(thread);
		const original = document.createElement('div'); original.setAttribute('data-discussion-original', '');
		const text = document.createElement('div'); text.className = 'ct-markdown'; text.style.lineHeight = '20px'; original.append(text);
		const pinned = document.createElement('div'); pinned.setAttribute('data-autosize-sticky', ''); pinned.style.position = 'sticky'; thread.append(original, pinned);
		const node = field(() => 640, 'none'); node.style.lineHeight = '20px'; pinned.append(node);
		Object.defineProperty(owner, 'clientHeight', { get: () => 200 });
		Object.defineProperty(original, 'offsetHeight', { get: () => Number.parseFloat(original.style.maxHeight) || 200 });
		Object.defineProperty(thread, 'offsetHeight', { get: () => 80 + original.offsetHeight + (Number.parseFloat(node.style.height) || 0) });
		const action = autosize(node, { value: 'a long first reply', fit: true });
		expect(original.style.maxHeight).toBe('60px');
		expect(original.getAttribute('tabindex')).toBe('0');
		expect(node.style.height).toBe('60px');
		action.destroy();
	});

	it('fits a margin card within the scroller while reserving room for its chrome', () => {
		const owner = document.createElement('div');
		owner.style.overflowY = 'auto';
		document.body.append(owner);
		const card = document.createElement('div');
		card.setAttribute('data-autosize-room', '');
		owner.append(card);
		const node = field(() => 640, 'none');
		node.style.lineHeight = '20px';
		card.append(node);
		Object.defineProperty(owner, 'clientHeight', { get: () => 300, configurable: true });
		Object.defineProperty(card, 'offsetHeight', { get: () => 120 + (Number.parseFloat(node.style.height) || 0), configurable: true });
		autosize(node, { value: 'a pasted page', fit: true });
		expect(node.style.height).toBe('180px');
		expect(node.style.overflowY).toBe('auto');
	});

	it('reserves the scroller padding when fitting a drawer reply', () => {
		const owner = document.createElement('div');
		owner.style.overflowY = 'auto';
		owner.style.paddingBottom = '16px';
		document.body.append(owner);
		const card = document.createElement('div');
		card.setAttribute('data-autosize-room', '');
		owner.append(card);
		const node = field(() => 640, 'none');
		node.style.lineHeight = '20px';
		card.append(node);
		Object.defineProperty(owner, 'clientHeight', { get: () => 300, configurable: true });
		Object.defineProperty(card, 'offsetHeight', { get: () => 120 + (Number.parseFloat(node.style.height) || 0), configurable: true });
		autosize(node, { value: 'a pasted page', fit: true });
		expect(node.style.height).toBe('160px');
	});

	it('fits a margin card within its smaller visible view', () => {
		const owner = document.createElement('div');
		owner.style.overflowY = 'auto';
		document.body.append(owner);
		const card = document.createElement('div');
		card.setAttribute('data-autosize-room', '');
		card.style.setProperty('--autosize-room', '220px');
		owner.append(card);
		const node = field(() => 640, 'none');
		node.style.lineHeight = '20px';
		card.append(node);
		Object.defineProperty(owner, 'clientHeight', { get: () => 300, configurable: true });
		Object.defineProperty(card, 'offsetHeight', { get: () => 120 + (Number.parseFloat(node.style.height) || 0), configurable: true });
		autosize(node, { value: 'a pasted page', fit: true });
		expect(node.style.height).toBe('100px');
		expect(node.style.overflowY).toBe('auto');
	});

	it('without fit and no cap takes its whole text', () => {
		const owner = document.createElement('div');
		owner.style.overflowY = 'auto';
		document.body.append(owner);
		Object.defineProperty(owner, 'clientHeight', { get: () => 300, configurable: true });
		Object.defineProperty(owner, 'scrollHeight', { get: () => 2000, configurable: true });
		const node = field(() => 640, 'none');
		owner.append(node);
		autosize(node, 'a pasted page');
		expect(node.style.height).toBe('640px');
		expect(node.style.overflowY).toBe('hidden');
	});

	it('brings the caret at the end back into view when a paste takes the field past its cap', () => {
		let tall = 54;
		const node = field(() => tall);
		node.value = 'short';
		const action = autosize(node, 'short');
		node.focus();
		node.value = 'a pasted page'; node.setSelectionRange(node.value.length, node.value.length);
		tall = 240;
		let written = -1;
		Object.defineProperty(node, 'scrollTop', { get: () => (written < 0 ? 0 : written), set: (value: number) => { written = value; }, configurable: true });
		action.update?.('a pasted page');
		expect(node.style.overflowY).toBe('auto');
		expect(written).toBe(240);
	});

	it('restores the caret after pasting into an already scrolling field', () => {
		const node = field(() => 240);
		node.value = 'first long draft';
		const action = autosize(node, node.value);
		node.focus();
		node.value = 'first long draft followed by a paste';
		node.setSelectionRange(node.value.length, node.value.length);
		node.scrollTop = 0;
		action.update(node.value);
		expect(node.scrollTop).toBe(240);
	});

	it('refits when the value changes, and leaves the transition to CSS', () => {
		let tall = 54;
		const node = field(() => tall);
		const action = autosize(node, 'one');
		tall = 36;
		action.update('');
		expect(node.style.height).toBe('36px');
		expect(node.style.transition).toBe('');
	});

	/** A just-opened thread card in a panel that is as tall as its content (237px) and may grow to `cap`. */
	function openedCard(cap: string) {
		const owner = document.createElement('div'); owner.style.overflowY = 'auto'; owner.style.maxHeight = cap; document.body.append(owner);
		const thread = document.createElement('div'); thread.setAttribute('data-autosize-room', ''); owner.append(thread);
		const original = document.createElement('div'); original.setAttribute('data-discussion-original', '');
		const text = document.createElement('div'); text.className = 'ct-markdown'; text.style.lineHeight = '20px'; original.append(text);
		const replies = document.createElement('div'); replies.setAttribute('data-discussion-replies', '');
		const reply = document.createElement('div'); reply.className = 'ct-reply'; const replyText = document.createElement('div'); replyText.className = 'ct-markdown'; replyText.style.lineHeight = '20px'; reply.append(replyText); replies.append(reply);
		const pinned = document.createElement('div'); pinned.setAttribute('data-autosize-sticky', ''); pinned.style.position = 'sticky';
		thread.append(original, replies, pinned);
		const node = field(() => 32, 'none'); node.style.lineHeight = '20px'; pinned.append(node);
		const fixed = (el: HTMLElement, h: number) => Object.defineProperty(el, 'offsetHeight', { get: () => Number.parseFloat(el.style.maxHeight) || h, configurable: true });
		fixed(original, 55); fixed(replies, 110);
		Object.defineProperty(thread, 'offsetHeight', { get: () => 40 + original.offsetHeight + replies.offsetHeight + (Number.parseFloat(node.style.height) || 0), configurable: true });
		Object.defineProperty(owner, 'clientHeight', { get: () => Math.min(thread.offsetHeight, Number.parseFloat(cap)), configurable: true });
		Object.defineProperty(owner, 'offsetHeight', { get: () => owner.clientHeight, configurable: true });
		return { original, replies, action: autosize(node, { value: '', fit: true }) };
	}
	it('leaves a just-opened card its whole discussion when its panel has room to grow to its max-height', () => {
		const { original, replies, action } = openedCard('400px');
		expect(original.style.maxHeight).toBe('');
		expect(replies.style.maxHeight).toBe('');
		action.destroy();
	});
	it('ignores an ancestor max-height that is a percentage rather than reading it as pixels', () => {
		const wrap = document.createElement('div'); wrap.style.maxHeight = '100%'; Object.defineProperty(wrap, 'offsetHeight', { get: () => 500, configurable: true }); document.body.append(wrap);
		const { original, replies, action } = openedCard('400px');
		wrap.append(original.closest('[data-autosize-room]')!.parentElement!);
		action.update({ value: '', fit: true });
		expect(original.style.maxHeight).toBe('');
		expect(replies.style.maxHeight).toBe('');
		action.destroy();
	});
	it('scrolls the discussion inside a card taller than its panel may grow', () => {
		const { original, replies, action } = openedCard('180px');
		expect(Number.parseFloat(replies.style.maxHeight)).toBeLessThan(110);
		expect(Number.parseFloat(replies.style.maxHeight)).toBeGreaterThan(0);
		expect(replies.getAttribute('tabindex')).toBe('0');
		expect(original.style.maxHeight).not.toBe('');
		action.destroy();
	});

	it('holds its room across refits when the panel is sized by its own content', () => {
		const owner = document.createElement('div'); owner.style.overflowY = 'auto'; document.body.append(owner);
		const thread = document.createElement('div'); thread.setAttribute('data-autosize-room', ''); owner.append(thread);
		const original = document.createElement('div'); original.setAttribute('data-discussion-original', '');
		const text = document.createElement('div'); text.className = 'ct-markdown'; text.style.lineHeight = '20px'; original.append(text);
		const pinned = document.createElement('div'); pinned.setAttribute('data-autosize-sticky', ''); pinned.style.position = 'sticky';
		thread.append(original, pinned);
		const node = field(() => 640, 'none'); node.style.lineHeight = '20px'; pinned.append(node);
		// A drawer panel sized by its content up to a cap: the view follows the card, so a refit that measured the
		// view before releasing the previous bound would shrink the room it hands to the next one.
		Object.defineProperty(owner, 'clientHeight', { get: () => Math.min(thread.offsetHeight, 320), configurable: true });
		Object.defineProperty(original, 'offsetHeight', { get: () => Number.parseFloat(original.style.maxHeight) || 200, configurable: true });
		Object.defineProperty(thread, 'offsetHeight', { get: () => 80 + original.offsetHeight + (Number.parseFloat(node.style.height) || 0), configurable: true });
		const action = autosize(node, { value: 'a long reply', fit: true });
		const first = { field: node.style.height, original: original.style.maxHeight };
		action.update({ value: 'a long reply', fit: true });
		action.update({ value: 'a long reply', fit: true });
		expect({ field: node.style.height, original: original.style.maxHeight }).toEqual(first);
		// The bound is real (below the 200px the text wants) and never below one line, and refits neither ratchet
		// it down nor hand the field room that is not there.
		expect(Number.parseFloat(first.original)).toBeLessThan(200);
		expect(Number.parseFloat(first.original)).toBeGreaterThanOrEqual(20);
		expect(Number.parseFloat(first.field)).toBeGreaterThan(0);
		action.destroy();
	});

	it('waits for the height ease before a refit, so the ease is not aborted', async () => {
		let view = 200; const refit: Array<() => void> = [];
		const ResizeObserver = class {
			constructor(callback: () => void) { refit.push(callback); }
			observe() {} disconnect() {}
		};
		(globalThis as Record<string, unknown>).ResizeObserver = ResizeObserver;
		const owner = document.createElement('div'); owner.style.overflowY = 'auto'; document.body.append(owner);
		const thread = document.createElement('div'); thread.setAttribute('data-autosize-room', ''); owner.append(thread);
		const pinned = document.createElement('div'); pinned.setAttribute('data-autosize-sticky', ''); pinned.style.position = 'sticky';
		thread.append(pinned);
		const node = field(() => 100, 'none'); node.style.lineHeight = '20px'; pinned.append(node);
		Object.defineProperty(owner, 'clientHeight', { get: () => view, configurable: true });
		Object.defineProperty(thread, 'offsetHeight', { get: () => 120, configurable: true });
		let easeRunning = true, settleEase: () => void = () => {};
		const finished = new Promise<void>((resolve) => { settleEase = resolve; });
		// A finished ease stays listed for a frame, so only a running one may hold the refit.
		node.getAnimations = () => [{ finished, playState: easeRunning ? 'running' : 'finished' } as unknown as Animation];
		const action = autosize(node, { value: 'a reply', fit: true });
		const fitted = node.style.height;
		// The view shrinks while the field is still easing to its last height: measuring now would write over the ease.
		view = 60;
		refit[0]?.();
		await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
		expect(node.style.height).toBe(fitted);
		// The ease ends without a `transitionend` — a replaced transition never fires one — and its record lingers.
		easeRunning = false; settleEase();
		await new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
		expect(node.style.height).toBe('40px');
		action.destroy();
		delete (globalThis as Record<string, unknown>).ResizeObserver;
	});
});
