import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';

const mocks = vi.hoisted(() => ({ retryPlacement: vi.fn() }));
vi.mock('$lib/stores/docComments', () => ({ retryPlacement: mocks.retryPlacement }));

import CommentThread from './CommentThread.svelte';
import CommentList from './CommentList.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
const card = (overrides: Record<string, unknown> = {}) =>
	({ id: 'c1', anchor: 'beta', author: 'you', text: 'note', at: 'now', resolved: false, replies: [], ...overrides });
function render(view: typeof CommentThread | typeof CommentList, props: Record<string, unknown>) {
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(view as any, { target: host, props });
	flushSync();
}
const line = () => host.querySelector<HTMLElement>('[data-comment-placement]');
afterEach(() => { if (component) unmount(component); component = null; host?.remove(); mocks.retryPlacement.mockReset(); vi.useRealTimers(); });

describe('live comment placement on the card (4.2)', () => {
	it('delays the placement line so a fast placement does not flash or resize the sheet', async () => {
		vi.useFakeTimers();
		render(CommentThread, { comment: card({ placement: 'placing', drawn: false }) });
		expect(line()).toBeNull();
		await vi.advanceTimersByTimeAsync(200);
		expect(line()?.textContent?.trim()).toBe('Finding its place…');
		expect(host.querySelector('[data-placement-retry]')).toBeNull();
	});

	it('shows "Couldn\'t find its place" with Retry on a row never drawn, and Retry asks again', () => {
		render(CommentThread, { comment: card({ placement: 'not_placed', drawn: false }) });
		expect(line()?.textContent).toContain('Couldn’t find its place');
		host.querySelector<HTMLButtonElement>('[data-placement-retry]')!.click();
		expect(mocks.retryPlacement).toHaveBeenCalledWith('c1');
	});

	it.each([
		['a placed row', { placement: 'placed', drawn: true }],
		['a Not placed row that keeps its old placement', { placement: 'not_placed', drawn: true }],
		['a Placing row still drawn where it moved', { placement: 'placing', drawn: true }],
		['a row in a document that is not live', {}],
	])('shows no placement line for %s', (_name, placement) => {
		render(CommentThread, { comment: card(placement) });
		expect(line()).toBeNull();
	});

	it('delays the disabled Jump description with the placement line', async () => {
		vi.useFakeTimers();
		render(CommentList, { comments: [card({ placement: 'placing', drawn: false }), card({ id: 'c2', placement: 'placed', drawn: true })], documentOrder: true });
		const [placing, placed] = [...host.querySelectorAll<HTMLButtonElement>('[data-comment-jump]')];
		expect(placing.disabled).toBe(true);
		expect(document.getElementById(placing.getAttribute('aria-describedby') ?? '')).toBeNull();
		await vi.advanceTimersByTimeAsync(200);
		const described = document.getElementById(placing.getAttribute('aria-describedby') ?? '');
		expect(described?.textContent?.trim()).toBe('Finding its place…');
		expect(placed.disabled).toBe(false);
	});
});
