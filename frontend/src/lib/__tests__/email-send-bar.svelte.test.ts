import { flushSync, mount, unmount } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import EmailSendBar from '$lib/components/EmailSendBar.svelte';
import EmailConversation from '$lib/components/EmailConversation.svelte';
import { toasts } from '$lib/stores/toast';
import { on } from '$lib/ws';

const { answerReview, fetchReviewBar, reload } = vi.hoisted(() => ({
	answerReview: vi.fn().mockResolvedValue({}), fetchReviewBar: vi.fn().mockResolvedValue({ ask_raised: false, hold_ids: [] }), reload: vi.fn().mockResolvedValue(undefined),
}));
vi.mock('$lib/api/reviewBar', async (importOriginal) => ({ ...(await importOriginal<typeof import('$lib/api/reviewBar')>()), answerReview, fetchReviewBar }));
vi.mock('$lib/stores/attentionDock.svelte', () => ({ reload }));
vi.mock('$lib/ws', () => ({ on: vi.fn(() => () => {}) }));

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

beforeEach(() => {
	vi.useFakeTimers();
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	vi.useRealTimers();
});

const bar = () => host.querySelector('.email-send-bar')!;
const button = (name: string) => [...host.querySelectorAll<HTMLButtonElement>('button')].find((b) => b.textContent?.trim() === name);
const press = (name: string) => { button(name)!.click(); flushSync(); };
const mountBar = (props: Record<string, unknown> = {}) => { component = mount(EmailSendBar, { target: host, props }); flushSync(); };

describe('EmailSendBar', () => {
	it('offers Don’t send and Send on a draft that can be sent', () => {
		mountBar();
		expect(button('Send')).toBeTruthy();
		expect(button('Don’t send')).toBeTruthy();
		expect(bar().querySelector('.bar-text')).toBeNull();
	});

	it('Send holds for five seconds with Undo in the bar and no toast over the header, then reads Sent', () => {
		mountBar();
		press('Send');
		expect(bar().textContent).toContain('Sending…');
		expect(button('Undo')).toBeTruthy();
		expect(get(toasts).some((t) => t.message === 'Sending…')).toBe(false);
		vi.advanceTimersByTime(4900);
		flushSync();
		expect(bar().textContent).toContain('Sending…');
		vi.advanceTimersByTime(200);
		flushSync();
		expect(bar().textContent).toContain('Sent');
	});

	it('Undo returns to an unsent draft with Send back', () => {
		mountBar();
		press('Send');
		press('Undo');
		expect(button('Send')).toBeTruthy();
		expect(bar().textContent).not.toContain('Sending…');
		expect(get(toasts).some((t) => t.message === 'Sending…')).toBe(false);
	});

	describe('while the bar is not on screen', () => {
		let watchers: Array<(entries: Array<{ isIntersecting: boolean }>) => void> = [];
		beforeEach(() => {
			watchers = [];
			vi.stubGlobal('IntersectionObserver', class {
				constructor(callback: (entries: Array<{ isIntersecting: boolean }>) => void) { watchers.push(callback); }
				observe() {}
				disconnect() {}
			});
		});
		afterEach(() => { vi.unstubAllGlobals(); toasts.unpin('email-send'); });
		const setOnScreen = (value: boolean) => { watchers.forEach((callback) => callback([{ isIntersecting: value }])); flushSync(); };
		const sendingToast = () => get(toasts).find((t) => t.message === 'Sending…');

		it('offers the same Undo in an ordinary toast, only while the bar is hidden', () => {
			mountBar();
			press('Send');
			expect(sendingToast()).toBeUndefined();
			setOnScreen(false);
			expect(sendingToast()?.action?.label).toBe('Undo');
			setOnScreen(true);
			expect(sendingToast()).toBeUndefined();
		});

		it('the toast Undo ends the hold like the bar Undo', () => {
			mountBar();
			press('Send');
			setOnScreen(false);
			sendingToast()!.action!.run();
			flushSync();
			expect(button('Send')).toBeTruthy();
			expect(sendingToast()).toBeUndefined();
			vi.advanceTimersByTime(6000);
			expect(bar().textContent).not.toContain('Sent');
		});

		it('the toast lasts only what is left of the five seconds', () => {
			mountBar();
			press('Send');
			vi.advanceTimersByTime(3000);
			setOnScreen(false);
			expect(sendingToast()?.duration).toBeLessThanOrEqual(2000);
			expect(sendingToast()?.duration).toBeGreaterThan(1500);
		});

		it('no toast once the email has left', () => {
			mountBar();
			press('Send');
			setOnScreen(false);
			vi.advanceTimersByTime(5100);
			flushSync();
			expect(bar().textContent).toContain('Sent');
			expect(sendingToast()).toBeUndefined();
		});
	});

	it('a failed send keeps the draft and offers Don’t send and Retry; Retry holds again', () => {
		mountBar({ sendEnds: 'failed' });
		press('Send');
		vi.advanceTimersByTime(5100);
		flushSync();
		expect(bar().querySelector('[role="alert"]')?.textContent).toBe('Couldn’t send. The email didn’t leave, and the draft is kept.');
		expect(button('Don’t send')).toBeTruthy();
		press('Retry');
		expect(bar().textContent).toContain('Sending…');
	});

	it('names the unavailable attachment and keeps Send from acting', () => {
		mountBar({ blocker: 'volunteer-waiver.pdf' });
		expect(bar().textContent).toContain('volunteer-waiver.pdf is unavailable. Remove it to send.');
		const send = button('Send')!;
		expect(send.getAttribute('aria-disabled')).toBe('true');
		send.click();
		flushSync();
		expect(bar().textContent).not.toContain('Sending…');
	});

	it('offline says why and disables both actions', () => {
		mountBar({ connected: false });
		expect(bar().textContent).toContain('Offline. Send is available when you reconnect.');
		expect(button('Don’t send')!.disabled).toBe(true);
		expect(button('Send')!.getAttribute('aria-disabled')).toBe('true');
	});

	it('read-only shows the reason and no actions', () => {
		mountBar({ readOnly: true });
		expect(bar().textContent).toContain('Read-only. This draft can’t be sent from here.');
		expect(button('Send')).toBeUndefined();
		expect(button('Don’t send')).toBeUndefined();
	});

	it('Don’t send asks once, and Keep draft goes back', () => {
		mountBar();
		press('Don’t send');
		expect(bar().textContent).toContain('Don’t send this email?');
		press('Keep draft');
		expect(button('Send')).toBeTruthy();
		press('Don’t send');
		press('Don’t send');
		expect(bar().textContent).toContain('Not sent.');
	});
});

describe('EmailSendBar settling a review item', () => {
	const asker = { id: 'email', name: 'Email drafter' };
	const sendRequest = (id: string) => ({ hold_id: id, asker, reason: 'Review the reply', purpose: 'send' as const });
	const ordinaryRequest = (id: string) => ({ hold_id: id, asker: { id: 'crew', name: 'Trail crew' }, reason: 'Check the parking' });
	const readOf = (...requests: Array<ReturnType<typeof sendRequest> | ReturnType<typeof ordinaryRequest>>) => ({ ask_raised: requests.length > 0, hold_ids: requests.map((r) => r.hold_id), requests });
	beforeEach(() => {
		answerReview.mockReset().mockResolvedValue({});
		fetchReviewBar.mockReset().mockResolvedValue(readOf(sendRequest('hold-1')));
		reload.mockClear();
	});
	const settle = async () => { await vi.advanceTimersByTimeAsync(5100); flushSync(); };
	const stale = async () => new (await import('$lib/api/reviewBar')).ReviewRefusal('ask_stale', 'replaced');
	const mountSettled = async (props: Record<string, unknown> = {}) => { mountBar({ reviewArtifactId: 'art-1', ...props }); await vi.advanceTimersByTimeAsync(0); flushSync(); };

	it('Send answers the send request as approved with the holds it knew at the press, then re-reads the queue', async () => {
		await mountSettled();
		press('Send');
		expect(answerReview).not.toHaveBeenCalled();
		await settle();
		expect(answerReview).toHaveBeenCalledWith('art-1', 'approved', '', ['hold-1']);
		expect(reload).toHaveBeenCalledTimes(1);
		expect(bar().textContent?.trim()).toBe('Sent');
	});

	it('answers only the send request when an ordinary request is open beside it', async () => {
		fetchReviewBar.mockResolvedValue(readOf(ordinaryRequest('hold-ord'), sendRequest('hold-1')));
		await mountSettled();
		press('Send');
		await settle();
		expect(answerReview).toHaveBeenCalledTimes(1);
		expect(answerReview).toHaveBeenCalledWith('art-1', 'approved', '', ['hold-1']);
	});

	it('answers nothing when no request is marked as the send request', async () => {
		fetchReviewBar.mockResolvedValue(readOf(ordinaryRequest('hold-ord')));
		await mountSettled();
		press('Send');
		await settle();
		expect(answerReview).not.toHaveBeenCalled();
		expect(bar().textContent?.trim()).toBe('Sent');
	});

	it('shows the answer is pending while it is on its way', async () => {
		answerReview.mockReturnValue(new Promise(() => {}));
		await mountSettled();
		press('Send');
		await settle();
		expect(bar().textContent).toContain('Sent. Updating the review…');
	});

	it('a failed answer leaves the email Sent and offers only Mark as answered, which retries the same holds', async () => {
		answerReview.mockRejectedValueOnce(new Error('503'));
		await mountSettled();
		press('Send');
		await settle();
		expect(bar().textContent).toContain('Sent. Couldn’t mark the review as answered.');
		expect([...bar().querySelectorAll('button')].map((b) => b.textContent?.trim())).toEqual(['Mark as answered']);
		fetchReviewBar.mockResolvedValue(readOf(sendRequest('hold-2')));
		press('Mark as answered');
		await vi.advanceTimersByTimeAsync(0);
		flushSync();
		expect(answerReview).toHaveBeenCalledTimes(2);
		expect(answerReview).toHaveBeenLastCalledWith('art-1', 'approved', '', ['hold-1']);
		expect(bar().textContent?.trim()).toBe('Sent');
	});

	it('refused as stale with no request left says the review ended, with no button', async () => {
		answerReview.mockRejectedValueOnce(await stale());
		await mountSettled();
		fetchReviewBar.mockResolvedValue(readOf());
		press('Send');
		await settle();
		expect(bar().textContent).toContain('Sent. The review request has ended.');
		expect(bar().querySelectorAll('button')).toHaveLength(0);
	});

	it('a request that replaced the answered one never inherits the answer: no button, no second answer, and the bar says one waits', async () => {
		answerReview.mockRejectedValueOnce(await stale());
		await mountSettled();
		press('Send');
		fetchReviewBar.mockResolvedValue(readOf(ordinaryRequest('hold-2'))); // the replacement lands during the five seconds
		await settle();
		expect(answerReview).toHaveBeenCalledTimes(1);
		expect(answerReview).toHaveBeenCalledWith('art-1', 'approved', '', ['hold-1']);
		expect(bar().textContent).toContain('Sent. The earlier review request ended. A new request is waiting in the review bar.');
		expect(bar().querySelectorAll('button')).toHaveLength(0);
	});

	it('an ordinary request that was already open does not read as a new one', async () => {
		fetchReviewBar.mockResolvedValue(readOf(ordinaryRequest('hold-ord'), sendRequest('hold-1')));
		answerReview.mockRejectedValueOnce(await stale());
		await mountSettled();
		press('Send');
		fetchReviewBar.mockResolvedValue(readOf(ordinaryRequest('hold-ord')));
		await settle();
		expect(bar().textContent).toContain('Sent. The review request has ended.');
	});

	// Not knowing is not knowing there is none: the bar answers nothing and says the request wasn't answered. It points at the review bar only when that bar shows the request with its answers (`answersShown`).
	const UNANSWERED = 'The review request wasn’t answered.';
	const POINTER = 'Answer it in the review bar.';
	it('a read that failed before the press answers nothing and says so, with no button, even when a later read succeeds', async () => {
		fetchReviewBar.mockRejectedValue(new Error('503'));
		await mountSettled();
		press('Send');
		fetchReviewBar.mockResolvedValue(readOf(sendRequest('hold-1'))); // a later read is not a snapshot of the press
		await settle();
		expect(answerReview).not.toHaveBeenCalled();
		expect(bar().textContent).toContain(`Sent. ${UNANSWERED}`);
		expect(bar().textContent).not.toContain(POINTER);
		expect(bar().querySelectorAll('button')).toHaveLength(0);
	});

	it('says where to answer only while the review bar shows the request with its answers', async () => {
		fetchReviewBar.mockRejectedValue(new Error('503'));
		await mountSettled({ answersShown: true });
		press('Send');
		await settle();
		expect(bar().textContent).toContain(`Sent. ${UNANSWERED} ${POINTER}`);
		expect(answerReview).not.toHaveBeenCalled();
	});

	it('Don’t send with the requests not known and no answers shown does not point at the review bar', async () => {
		fetchReviewBar.mockRejectedValue(new Error('503'));
		await mountSettled();
		press('Don’t send');
		press('Don’t send');
		await vi.advanceTimersByTimeAsync(0);
		flushSync();
		expect(bar().textContent).toContain(`Not sent. ${UNANSWERED}`);
		expect(bar().textContent).not.toContain(POINTER);
	});

	it('a first read still on its way at the press is not known either: nothing is answered', async () => {
		fetchReviewBar.mockReturnValue(new Promise(() => {}));
		await mountSettled();
		press('Send');
		await settle();
		expect(answerReview).not.toHaveBeenCalled();
		expect(bar().textContent).toContain(`Sent. ${UNANSWERED}`);
	});

	it('Don’t send with the requests not known answers nothing and says so', async () => {
		fetchReviewBar.mockRejectedValue(new Error('503'));
		await mountSettled();
		press('Don’t send');
		press('Don’t send');
		await vi.advanceTimersByTimeAsync(0);
		flushSync();
		expect(answerReview).not.toHaveBeenCalled();
		expect(bar().textContent).toContain(`Not sent. ${UNANSWERED}`);
	});

	it('the unanswered sentence goes once a completed read shows no send request open', async () => {
		fetchReviewBar.mockRejectedValue(new Error('503'));
		await mountSettled();
		press('Send');
		await settle();
		expect(bar().textContent).toContain(UNANSWERED);
		fetchReviewBar.mockResolvedValue(readOf()); // the person answered it in the review bar
		for (const [type, handler] of vi.mocked(on).mock.calls) if (type === 'plans_changed') (handler as () => void)();
		await vi.advanceTimersByTimeAsync(200);
		flushSync();
		expect(bar().textContent?.trim()).toBe('Sent');
		expect(answerReview).not.toHaveBeenCalled();
	});

	it('a read that failed after an earlier one succeeded keeps the earlier holds', async () => {
		await mountSettled();
		fetchReviewBar.mockRejectedValue(new Error('503'));
		press('Send');
		await settle();
		expect(answerReview).toHaveBeenCalledWith('art-1', 'approved', '', ['hold-1']);
	});

	it('Undo settles nothing', () => {
		mountBar({ reviewArtifactId: 'art-1' });
		press('Send');
		press('Undo');
		vi.advanceTimersByTime(6000);
		expect(answerReview).not.toHaveBeenCalled();
	});

	it('a failed send settles nothing', () => {
		mountBar({ reviewArtifactId: 'art-1', sendEnds: 'failed' });
		press('Send');
		vi.advanceTimersByTime(5100);
		flushSync();
		expect(answerReview).not.toHaveBeenCalled();
	});

	it('a confirmed Don’t send answers changes requested with the fixed note; Keep draft settles nothing', async () => {
		await mountSettled();
		press('Don’t send');
		press('Keep draft');
		expect(answerReview).not.toHaveBeenCalled();
		press('Don’t send');
		press('Don’t send');
		await vi.advanceTimersByTimeAsync(0);
		expect(answerReview).toHaveBeenCalledWith('art-1', 'changes_requested', 'This email was marked not to send.', ['hold-1']);
	});

	it('releases the canvas to answer a send request only when this bar holds no answer for it', async () => {
		let free = false;
		const bound = { reviewArtifactId: 'art-1', get answersFree() { return free; }, set answersFree(value: boolean) { free = value; } };
		answerReview.mockRejectedValueOnce(new Error('503'));
		mountBar(bound); // not through mountSettled: spreading would drop the setter
		await vi.advanceTimersByTimeAsync(0);
		flushSync();
		expect(free).toBe(false);
		press('Send');
		await settle();
		expect(bar().textContent).toContain('Couldn’t mark the review as answered.');
		expect(free).toBe(false);
		fetchReviewBar.mockResolvedValue(readOf());
		press('Mark as answered');
		await vi.advanceTimersByTimeAsync(0);
		flushSync();
		expect(bar().textContent?.trim()).toBe('Sent');
		expect(free).toBe(true);
	});

	it('reports the end once the email is sent or marked not to send, and not before', () => {
		let ended = false;
		const bound = { get ended() { return ended; }, set ended(value: boolean) { ended = value; } };
		mountBar(bound);
		expect(ended).toBe(false);
		press('Send');
		vi.advanceTimersByTime(1000);
		flushSync();
		expect(ended).toBe(false);
		vi.advanceTimersByTime(4100);
		flushSync();
		expect(ended).toBe(true);
	});

	it('reports the end for a draft marked not to send', () => {
		let ended = false;
		mountBar({ get ended() { return ended; }, set ended(value: boolean) { ended = value; } });
		press('Don’t send');
		expect(ended).toBe(false);
		press('Don’t send');
		expect(ended).toBe(true);
	});

	it('a draft with no review item calls nothing', () => {
		mountBar();
		press('Send');
		vi.advanceTimersByTime(5100);
		flushSync();
		expect(bar().textContent).toContain('Sent');
		expect(answerReview).not.toHaveBeenCalled();
	});
});

describe('EmailConversation comments', () => {
	it('shows a comment under the email it was written on, with its quoted text', () => {
		const messages = [{ id: 'm1', sender: 'Marcus Obi <marcus@example.com>', date: '2026-11-03T09:40:00Z', body: 'Does anyone know if the gate code changed?' }];
		component = mount(EmailConversation, { target: host, props: { messages, comments: [{ messageId: 'm1', quote: 'gate code changed', text: 'Please check with the county.' }] } });
		flushSync();
		const toggle = host.querySelector<HTMLButtonElement>('.email-toggle')!;
		toggle.click();
		flushSync();
		expect(host.textContent).toContain('Please check with the county.');
		expect(host.textContent).toContain('gate code changed');
	});
});
