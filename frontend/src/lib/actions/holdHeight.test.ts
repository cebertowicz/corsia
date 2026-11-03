import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { holdHeight } from './holdHeight';

/** A holder with one content child whose height and own offsetTop the test sets, and a ResizeObserver it can fire. */
function holder() {
	let notify: () => void = () => {};
	vi.stubGlobal('ResizeObserver', class { constructor(cb: () => void) { notify = cb; } observe() {} disconnect() {} });
	const node = document.createElement('div');
	const content = document.createElement('div');
	node.append(content);
	document.body.append(node);
	const state = { height: 0, top: 500 };
	content.getBoundingClientRect = () => ({ height: state.height }) as DOMRect;
	Object.defineProperty(node, 'offsetTop', { get: () => state.top });
	const grow = (height: number) => { state.height = height; notify(); };
	return { node, state, grow };
}

describe('holdHeight', () => {
	beforeEach(() => { vi.useFakeTimers(); });
	afterEach(() => {
		vi.useRealTimers();
		vi.unstubAllGlobals();
		document.body.innerHTML = '';
	});

	it('ratchets min-height up to the tallest the content has been, never down, while held', () => {
		const { node, grow } = holder();
		const action = holdHeight(node, { hold: true, ratchet: true });
		grow(80);
		grow(40);
		expect(node.style.minHeight).toBe('80px');
		grow(120);
		expect(node.style.minHeight).toBe('120px');
		action.destroy();
	});

	it('does not ratchet while the streamed reply is shown', () => {
		const { node, grow } = holder();
		const action = holdHeight(node, { hold: true, ratchet: false });
		grow(200);
		expect(node.style.minHeight).toBe('');
		action.destroy();
	});

	it('gives the room back as a glide when the hold ends, then clears it', () => {
		const { node, grow } = holder();
		const action = holdHeight(node, { hold: true, ratchet: true });
		grow(90);
		action.update({ hold: false, ratchet: true });
		expect(node.style.transition).toContain('min-height');
		expect(node.style.minHeight).toBe('0px');
		vi.advanceTimersByTime(500);
		expect(node.style.minHeight).toBe('');
		expect(node.style.transition).toBe('');
		action.destroy();
	});

	it('Focus: adds how far the node moved up since `before` to the room it gives back', () => {
		const { node, state } = holder();
		const action = holdHeight(node, { hold: true, ratchet: true, before: 500 });
		state.top = 416; // the open block became shorter finished rows: the node sits 84px higher
		let room = '';
		// The glide starts from the room: it is what min-height reads when layout is forced.
		Object.defineProperty(node, 'offsetHeight', { get: () => { room = node.style.minHeight; return 0; } });
		action.update({ hold: false, ratchet: true, before: 500 });
		expect(room).toBe('84px');
		action.destroy();
	});

	it('Focus: gives back nothing when the node did not move up', () => {
		const { node, state } = holder();
		const action = holdHeight(node, { hold: true, ratchet: true, before: 500 });
		state.top = 530; // moved down: nothing was taken away
		action.update({ hold: false, ratchet: true, before: 500 });
		expect(node.style.minHeight).toBe('');
		expect(node.style.transition).toBe('');
		action.destroy();
	});

	it('a reply that clears gives up the height it took, so nothing below it moves', () => {
		const { node, state, grow } = holder();
		const action = holdHeight(node, { hold: true, ratchet: true });
		grow(150);
		action.update({ hold: true, ratchet: false });
		state.top = 560; // the reply landed above the node: 60px
		action.update({ hold: true, ratchet: true });
		expect(node.style.minHeight).toBe('90px');
		action.destroy();
	});

	it('a new turn that starts mid-glide keeps the height the glide had reached', () => {
		const { node, grow } = holder();
		const action = holdHeight(node, { hold: true, ratchet: true });
		grow(100);
		action.update({ hold: false, ratchet: true });
		node.getBoundingClientRect = () => ({ height: 70 }) as DOMRect;
		action.update({ hold: true, ratchet: true });
		expect(node.style.minHeight).toBe('70px');
		expect(node.style.transition).toBe('');
		action.destroy();
	});
});
