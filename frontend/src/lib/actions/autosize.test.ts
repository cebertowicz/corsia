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
