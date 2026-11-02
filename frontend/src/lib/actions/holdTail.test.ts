import { afterEach, describe, expect, it, vi } from 'vitest';
import { anchorPrepend, contentEnd, holdTail } from './holdTail';
import { contentFitsView } from '../messagePager';
import { MessageListScrollControl } from '../components/messageListScrollControl';

/** A scroller whose layout is computed the way the browser does it: the taller of the content and the floor marker. */
function layout(pad = 100) {
	const scroller = document.createElement('div');
	scroller.style.paddingBottom = `${pad}px`;
	const end = document.createElement('div');
	end.className = 'tail-end';
	const marker = document.createElement('div');
	marker.className = 'tail-hold';
	scroller.append(end, marker);
	document.body.append(scroller);
	const state = { endTop: 500, top: 0 };
	const padNow = () => parseFloat(scroller.style.paddingBottom);
	const floorBottom = () => (parseFloat(marker.style.getPropertyValue('--tail-high')) || 0) + padNow();
	Object.defineProperty(end, 'offsetTop', { get: () => state.endTop });
	Object.defineProperty(marker, 'offsetHeight', { get: () => (marker.style.height ? 1 : 0) });
	Object.defineProperty(scroller, 'scrollHeight', { get: () => Math.max(state.endTop + padNow(), floorBottom()) });
	Object.defineProperty(scroller, 'clientHeight', { get: () => 300 });
	Object.defineProperty(scroller, 'scrollTop', {
		get: () => state.top,
		set: (v: number) => { state.top = Math.max(0, Math.min(v, scroller.scrollHeight - 300)); },
	});
	return { scroller, marker, state };
}

describe('holdTail: the one owner of the floor', () => {
	afterEach(() => { document.body.innerHTML = ''; });

	it('contentEnd is scrollHeight where no floor is mounted', () => {
		const plain = { scrollTop: 0, scrollHeight: 800, clientHeight: 300 };
		expect(contentEnd(plain)).toBe(800);
	});

	it('contentEnd is scrollHeight while the floor is not holding anything', () => {
		const { scroller, marker } = layout();
		const action = holdTail(marker, { key: 't', live: false });
		expect(contentEnd(scroller)).toBe(600);
		action.destroy();
	});

	it('contentEnd leaves out the blank space the floor holds when the content gets shorter', () => {
		const { scroller, marker, state } = layout();
		const action = holdTail(marker, { key: 't', live: true });
		expect(scroller.scrollHeight).toBe(600);
		state.endTop = 380;
		expect(scroller.scrollHeight).toBe(600);
		expect(contentEnd(scroller)).toBe(480);
		action.destroy();
	});

	it('contentEnd follows the content once it grows past the floor', () => {
		const { scroller, marker, state } = layout();
		const action = holdTail(marker, { key: 't', live: true });
		state.endTop = 700;
		expect(contentEnd(scroller)).toBe(800);
		action.destroy();
	});

	it('anchorPrepend keeps the text under a reader and carries the held blank space down with the content', () => {
		const { scroller, marker, state } = layout();
		const action = holdTail(marker, { key: 't', live: true });
		state.endTop = 380; // blank space held: 120
		scroller.scrollTop = 300;
		const keep = anchorPrepend(scroller);
		state.endTop += 200; // older history added above the content
		keep();
		expect(scroller.scrollTop).toBe(500);
		expect(contentEnd(scroller)).toBe(680);
		expect(scroller.scrollHeight - contentEnd(scroller)).toBe(120);
		action.destroy();
	});

	it('anchorPrepend keeps the text still when the history added is shorter than the blank space', () => {
		const { scroller, marker, state } = layout();
		const action = holdTail(marker, { key: 't', live: true });
		state.endTop = 380;
		scroller.scrollTop = 300;
		const keep = anchorPrepend(scroller);
		state.endTop += 50;
		keep();
		expect(scroller.scrollTop).toBe(350);
		expect(scroller.scrollHeight - contentEnd(scroller)).toBe(120);
		action.destroy();
	});

	it('anchorPrepend still works after the turn has ended and the floor stopped rising', () => {
		const { scroller, marker, state } = layout();
		const action = holdTail(marker, { key: 't', live: true });
		state.endTop = 380;
		action.update({ key: 't', live: false });
		scroller.scrollTop = 300;
		const keep = anchorPrepend(scroller);
		state.endTop += 200;
		keep();
		expect(scroller.scrollTop).toBe(500);
		expect(scroller.scrollHeight - contentEnd(scroller)).toBe(120);
		action.destroy();
	});

	it('anchorPrepend adds what the content grew by where there is no floor', () => {
		const el = { scrollTop: 40, clientHeight: 300, height: 800, get scrollHeight() { return this.height; } };
		const keep = anchorPrepend(el);
		el.height = 1000;
		keep();
		expect(el.scrollTop).toBe(240);
	});

	it('the view comes down with a band that shrinks while it reaches into the blank space, and no further than the content end', () => {
		let notify: () => void = () => {};
		vi.stubGlobal('ResizeObserver', class { constructor(cb: () => void) { notify = cb; } observe() {} disconnect() {} });
		try {
			const { scroller, marker, state } = layout(100);
			const action = holdTail(marker, { key: 't', live: true });
			state.endTop = 380; // the content ends at 480, the floor holds the scroller at 600
			scroller.scrollTop = 180; // a view that ends where the content does: 120px of floor below it
			scroller.style.paddingBottom = '70px'; // the band shrinks by 30; the floor is still below the view
			notify();
			expect(scroller.scrollTop).toBe(150); // the content end is at the bottom of the view again
			scroller.style.paddingBottom = '40px';
			notify();
			expect(scroller.scrollTop).toBe(120);
			scroller.scrollTop = 20; // a reader scrolled up is not in the blank space: nothing to shed
			scroller.style.paddingBottom = '10px';
			notify();
			expect(scroller.scrollTop).toBe(20);
			action.destroy();
		} finally {
			vi.unstubAllGlobals();
		}
	});

	it('destroy takes the scroller back to plain scrollHeight', () => {
		const { scroller, marker, state } = layout();
		const action = holdTail(marker, { key: 't', live: true });
		state.endTop = 380;
		action.destroy();
		expect(contentEnd(scroller)).toBe(scroller.scrollHeight);
	});

	it('the auto-page check reads where the content ends, not the floor', () => {
		const { scroller, marker, state } = layout();
		const action = holdTail(marker, { key: 't', live: true });
		state.endTop = 380; // content ends 180 below the top of a 300px view; the floor holds the scroller 300 above its end
		expect(scroller.scrollHeight - scroller.clientHeight).toBe(300);
		expect(contentFitsView(scroller)).toBe(true);
		state.endTop = 480;
		expect(contentFitsView(scroller)).toBe(false);
		action.destroy();
	});

	describe('the transcript scroll control on a floor', () => {
		function control(scroller: HTMLElement) {
			return new MessageListScrollControl(() => scroller, () => {}, () => {});
		}

		it('writeBottom lands on the content end and never backs up under blank space', () => {
			const { scroller, marker, state } = layout();
			const action = holdTail(marker, { key: 't', live: true });
			state.endTop = 380;
			scroller.scrollTop = 300; // the view reaches the floor: 120px of blank space in view
			control(scroller).writeBottom();
			expect(scroller.scrollTop).toBe(300);
			scroller.scrollTop = 0;
			control(scroller).writeBottom();
			expect(scroller.scrollTop).toBe(180);
			action.destroy();
		});

		it('writeBottom goes to the end where there is no floor', () => {
			const { scroller, state } = layout();
			state.endTop = 700;
			control(scroller).writeBottom();
			expect(scroller.scrollTop).toBe(500);
		});

		it('captures and restores the place with blank space in view, and without a floor', () => {
			const { scroller, marker, state } = layout();
			const action = holdTail(marker, { key: 't', live: true });
			state.endTop = 380;
			scroller.scrollTop = 300;
			const held = control(scroller);
			held.captureBottomOffset();
			scroller.scrollTop = 0;
			held.restoreBottomOffset();
			expect(scroller.scrollTop).toBe(300);
			scroller.scrollTop = 100;
			held.captureBottomOffset();
			scroller.scrollTop = 0;
			held.restoreBottomOffset();
			expect(scroller.scrollTop).toBe(100);
			action.destroy();
			const plain = layout();
			plain.state.endTop = 700;
			plain.scroller.scrollTop = 500;
			const bare = control(plain.scroller);
			bare.captureBottomOffset();
			plain.scroller.scrollTop = 0;
			bare.restoreBottomOffset();
			expect(plain.scroller.scrollTop).toBe(500);
		});

		it('reads the near-bottom state from where the content ends', () => {
			const { scroller, marker, state } = layout();
			const action = holdTail(marker, { key: 't', live: true });
			state.endTop = 380;
			scroller.scrollTop = 180; // the last message is at the bottom of the view, blank space below it
			const seen: boolean[] = [];
			new MessageListScrollControl(() => scroller, (near) => seen.push(near), () => {}).syncNearBottom();
			expect(seen).toEqual([true]);
			action.destroy();
		});
	});
});
