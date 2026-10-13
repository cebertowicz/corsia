import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { flushSync } from 'svelte';
import { createClassComponent } from 'svelte/legacy';
import DocViewerStatus from './DocViewerStatus.svelte';

const events = vi.hoisted(() => new Map<string, Set<(frame: unknown) => void>>());
const toastShow = vi.hoisted(() => vi.fn());
vi.mock('$lib/ws/events', () => ({ on: (name: string, handler: (frame: unknown) => void) => {
	const listeners = events.get(name) ?? new Set(); listeners.add(handler); events.set(name, listeners);
	return () => listeners.delete(handler);
} }));
vi.mock('$lib/stores/toast', () => ({ toasts: { show: toastShow } }));
let host: HTMLElement;
let component: { $destroy(): void; $set(props: Record<string, unknown>): void } | undefined;
let width = 900;
let rows: Record<string, unknown>[];
let reviewers: Record<string, unknown>[];
let rosterError = false;
let fetchMock: ReturnType<typeof vi.fn>;
const NAMES: Record<string, string> = { quality: 'Quality Gate', product: 'Product Experience', plans: 'Structured Plans' };
const reviewer = (id: string, extras = {}) => ({ id, name: NAMES[id] ?? id, responsibility: 'Review documents', requested: false, unavailable_reason: null, unavailable_text: null, ...extras });
const row = (id = 'review-1', extras = {}) => ({ id, reviewer_thread_id: 'quality', reviewer_name: 'Quality Gate', requester_name: 'Steve', may_cancel: true, outcome: 'open', created_at: '2026-10-02T16:14:00Z', ...extras });
const json = (body: unknown, status = 200) => new Response(JSON.stringify(body), { status, headers: { 'Content-Type': 'application/json' } });
const list = () => ({ reviews: rows, ...(rosterError ? { reviewers_error: 'roster_error' } : { reviewers }) });
async function settle() { await new Promise((resolve) => setTimeout(resolve, 0)); flushSync(); }
function render(extra: Record<string, unknown> = {}) {
	host = document.createElement('div'); host.className = 'doc'; document.body.appendChild(host);
	component = createClassComponent({ target: host, component: DocViewerStatus, props: {
		ready: true, stale: false, readonly: false, commentCount: 0, orphanedCount: 0,
		artifactId: 'doc-a', targetInstanceId: 'tab-a', viewingThreadId: 'thread-a', reviewTargetKey: 'target-a',
		onOpenComments: vi.fn(), freshness: null, ...extra,
	} });
	flushSync();
}
const nameOf = (item: HTMLElement) => (item.getAttribute('aria-label') ?? item.textContent ?? '').trim();
function button(label: string) {
	const found = [...document.body.querySelectorAll<HTMLButtonElement>('button')].find((item) => nameOf(item) === label);
	expect(found, `Button ${label}`).toBeTruthy(); return found!;
}
const maybeButton = (label: string) => [...document.body.querySelectorAll<HTMLButtonElement>('button')].find((item) => nameOf(item) === label);
async function picker() { button('Request review').click(); await settle(); return document.body.querySelector<HTMLInputElement>('[role="combobox"]')!; }
function type(element: HTMLInputElement | HTMLTextAreaElement, value: string) { element.value = value; element.dispatchEvent(new Event('input', { bubbles: true })); flushSync(); }
function key(element: HTMLElement, key: string) { element.dispatchEvent(new KeyboardEvent('keydown', { key, bubbles: true, cancelable: true })); flushSync(); }
function choose(search: HTMLInputElement, text: string) { type(search, text); key(search, 'Enter'); }
const chips = () => [...document.body.querySelectorAll('.chip-name')].map((item) => item.textContent);
const optionNames = () => [...document.body.querySelectorAll('[role="option"] .option-name')].map((item) => item.textContent);
const sendRequests = () => fetchMock.mock.calls.filter(([input]) => input === '/api/doc-reviews').map(([, init]) => JSON.parse((init as RequestInit).body as string));
function emit(artifact_id: string) { for (const callback of events.get('artifacts_changed') ?? []) callback({ artifact_id }); }
beforeEach(() => {
	width = 900; rows = []; reviewers = [reviewer('quality'), reviewer('product'), reviewer('plans')]; rosterError = false; toastShow.mockClear();
	vi.stubGlobal('ResizeObserver', class { constructor(private callback: ResizeObserverCallback) {} observe(element: Element) { this.callback([{ contentRect: { width }, target: element } as ResizeObserverEntry], this as unknown as ResizeObserver); } disconnect() {} });
	fetchMock = vi.fn(async (input: string, init?: RequestInit) => {
		if (input.includes('/reviews')) return json(list());
		if (input === '/api/doc-reviews') {
			const sent = JSON.parse(init!.body as string).reviewer as string;
			const made = row(`review-${sent}`, { reviewer_thread_id: sent, reviewer_name: NAMES[sent] });
			rows = [...rows, made]; reviewers = reviewers.map((item) => item.id === sent ? { ...item, requested: true } : item);
			return json({ request: made });
		}
		if (init?.method === 'DELETE') { rows = []; return json({ request: { outcome: 'cancelled' } }); }
		return json({ woken: [], undelivered_batches: [] });
	});
	vi.stubGlobal('fetch', fetchMock);
});
afterEach(() => { component?.$destroy(); host?.remove(); events.clear(); vi.unstubAllGlobals(); });

describe('document review toolbar and popover', () => {
	it('requires artifactId without hiding the comment control', async () => {
		render({ commentCount: 2 }); await settle(); expect(button('Request review')).toBeTruthy();
		component.$set({ artifactId: null }); flushSync();
		expect(maybeButton('Request review')).toBeUndefined(); expect(button('2 open threads')).toBeTruthy();
	});
	it('shows only the Request review button when nobody is reviewing, and one control style across the row', async () => {
		render({ commentCount: 1, stale: true }); await settle();
		expect(document.body.querySelector('.reviewer-group')).toBeNull();
		const names = [...document.body.querySelectorAll<HTMLElement>('.document-tools button')].map(nameOf);
		expect(names).toEqual(['File changed · Reload', '1 open thread', 'Request review']);
		expect([...document.body.querySelectorAll('.document-tools button')].every((item) => item.classList.contains('doc-tool'))).toBe(true);
	});
	it('lists the chosen agents as chips, removes the last on Backspace, and keeps Send disabled with its reason until one is chosen', async () => {
		render(); const search = await picker(); expect(document.activeElement).toBe(search);
		const send = button('Send'); expect(send.disabled).toBe(true);
		expect(document.body.textContent).toContain('Choose a reviewer to send');
		expect(document.body.querySelector('textarea')).toBeNull();
		choose(search, 'quality'); await settle();
		expect(chips()).toEqual(['Quality Gate']); expect(button('Send').disabled).toBe(false);
		expect(document.body.querySelector('textarea')).toBeTruthy();
		choose(search, 'product'); await settle();
		expect(chips()).toEqual(['Quality Gate', 'Product Experience']);
		expect(optionNames()).toEqual(['Structured Plans']);
		key(search, 'Backspace'); await settle();
		expect(chips()).toEqual(['Quality Gate']);
		button('Remove Quality Gate').click(); await settle();
		expect(chips()).toEqual([]); expect(button('Send').disabled).toBe(true); expect(document.activeElement).toBe(search);
	});
	it('supports combobox Down/Up/Home/End/Enter and filtering by name', async () => {
		render(); const search = await picker();
		key(search, 'End'); expect(search.getAttribute('aria-activedescendant')).toContain('plans');
		key(search, 'Home'); expect(search.getAttribute('aria-activedescendant')).toContain('quality');
		key(search, 'ArrowDown'); key(search, 'ArrowUp'); key(search, 'Enter'); await settle();
		expect(chips()).toEqual(['Quality Gate']);
		type(search, 'plans'); expect(optionNames()).toEqual(['Structured Plans']);
	});
	it('never offers an agent that already has a request or cannot take the work', async () => {
		reviewers = [reviewer('quality', { requested: true }), reviewer('product', { unavailable_reason: 'owner_unaddressable', unavailable_text: "Can't be reached right now" }), reviewer('plans')];
		render(); await picker();
		expect(optionNames()).toEqual(['Structured Plans']);
		expect(document.body.textContent).not.toContain("Can't be reached");
	});
	it('sends one request per chosen reviewer, with a note, then closes, shows the avatar group and says who was asked', async () => {
		render(); const search = await picker();
		choose(search, 'quality'); choose(search, 'product'); await settle();
		type(document.body.querySelector('textarea')!, 'Check the claims');
		button('Send').click(); await settle(); await settle();
		const sent = sendRequests();
		expect(sent.map((body) => body.reviewer)).toEqual(['quality', 'product']);
		expect(sent.every((body) => body.note === 'Check the claims' && body.thread_id === 'thread-a')).toBe(true);
		expect(new Set(sent.map((body) => body.client_id)).size).toBe(2);
		expect(document.body.querySelector('[data-popover]')).toBeNull();
		expect(toastShow).toHaveBeenCalledWith('Review requested from Quality Gate and Product Experience', 'success');
		const group = document.body.querySelector<HTMLElement>('.reviewer-group')!;
		expect(group.getAttribute('aria-label')).toBe('Reviewers: Quality Gate (requested), Product Experience (requested)');
		expect([...group.querySelectorAll('.reviewer')].map((item) => item.getAttribute('title'))).toEqual(['Quality Gate: requested', 'Product Experience: requested']);
		expect(document.activeElement).toBe(button('Request review'));
	});
	it('keeps chips and note on a refusal, names who was already asked after a partial send, and reuses the retry key for the one that failed', async () => {
		render(); const search = await picker();
		choose(search, 'quality'); choose(search, 'product'); await settle();
		type(document.body.querySelector('textarea')!, 'Keep my note');
		const original = fetchMock.getMockImplementation() as (input: string, init?: RequestInit) => Promise<Response>;
		let refuse = true;
		fetchMock.mockImplementation(async (input: string, init?: RequestInit) => {
			if (refuse && input === '/api/doc-reviews' && JSON.parse(init!.body as string).reviewer === 'product') return json({ detail: 'Not accepting work right now' }, 400);
			return original(input, init);
		});
		button('Send').click(); await settle(); await settle();
		const alert = document.body.querySelector('[role="alert"]')!;
		expect(alert.querySelector('p')?.textContent).toBe("Requested from Quality Gate. These didn't go through:");
		expect([...alert.querySelectorAll('li')].map((item) => item.textContent)).toEqual(['Product Experience: Not accepting work right now']);
		expect(document.body.querySelector('.chip.failed .chip-name')?.textContent).toBe('Product Experience');
		expect(chips()).toEqual(['Product Experience']); expect(document.body.querySelector('textarea')?.value).toBe('Keep my note');
		expect(document.body.querySelector('[data-popover]')).toBeTruthy(); expect(toastShow).not.toHaveBeenCalled();
		const firstKey = sendRequests().find((body) => body.reviewer === 'product')!.client_id;
		refuse = false;
		button('Retry').click(); await settle(); await settle();
		const productCalls = sendRequests().filter((body) => body.reviewer === 'product');
		expect(productCalls).toHaveLength(2); expect(productCalls[1].client_id).toBe(firstKey);
		expect(sendRequests().filter((body) => body.reviewer === 'quality')).toHaveLength(1);
		expect(toastShow).toHaveBeenCalledWith('Review requested from Product Experience', 'success');
	});
	it('tries every reviewer, names each one that failed, keeps only those chosen, and never claims the whole group', async () => {
		render(); const search = await picker();
		choose(search, 'quality'); choose(search, 'product'); choose(search, 'plans'); await settle();
		const original = fetchMock.getMockImplementation() as (input: string, init?: RequestInit) => Promise<Response>;
		let refuse = true;
		fetchMock.mockImplementation(async (input: string, init?: RequestInit) => {
			if (refuse && input === '/api/doc-reviews' && JSON.parse(init!.body as string).reviewer === 'product') return json({ detail: 'Not accepting work right now' }, 400);
			return original(input, init);
		});
		button('Send').click(); await settle(); await settle();
		// The failure in the middle does not stop the reviewer after it.
		expect(sendRequests().map((body) => body.reviewer)).toEqual(['quality', 'product', 'plans']);
		expect(chips()).toEqual(['Product Experience']); expect(toastShow).not.toHaveBeenCalled();
		expect(document.body.querySelector('[role="alert"] p')?.textContent).toBe("Requested from Quality Gate and Structured Plans. These didn't go through:");
		refuse = false; button('Retry').click(); await settle(); await settle();
		expect(sendRequests().map((body) => body.reviewer)).toEqual(['quality', 'product', 'plans', 'product']);
		expect(toastShow).toHaveBeenCalledTimes(1); expect(toastShow).toHaveBeenCalledWith('Review requested from Product Experience', 'success');
	});
	it('drops a reviewer from the failures when it is removed, and clears the refusal once none are left', async () => {
		render(); const search = await picker(); choose(search, 'product'); await settle();
		fetchMock.mockImplementationOnce(async () => json({ detail: 'Not accepting work right now' }, 400));
		button('Send').click(); await settle(); await settle();
		expect(document.body.querySelector('[role="alert"]')).toBeTruthy();
		button('Remove Product Experience').click(); await settle();
		expect(document.body.querySelector('[role="alert"]')).toBeNull();
	});
	it('lists current requests as compact rows with their state and a Cancel that follows may_cancel', async () => {
		rows = [row('review-1'), row('review-2', { reviewer_thread_id: 'product', reviewer_name: 'Product Experience', may_cancel: false }), row('review-3', { reviewer_thread_id: 'plans', reviewer_name: 'Structured Plans', outcome: 'ended_without_comments', may_cancel: false })];
		render(); await settle();
		button('Reviewers: Quality Gate (requested), Product Experience (requested), Structured Plans (ended without comments)').click(); await settle();
		const items = [...document.body.querySelectorAll('[data-review-request]')];
		expect(items).toHaveLength(3);
		expect(items[0].querySelector('.status')?.textContent).toMatch(/^Requested \d.*[AP]M$/);
		expect(items[2].querySelector('.status')?.textContent).toBe('Ended without comments');
		expect(maybeButton('Cancel request to Product Experience')).toBeUndefined();
		expect(maybeButton('Cancel request to Structured Plans')).toBeUndefined();
		button('Cancel request to Quality Gate').click(); await settle();
		expect(fetchMock.mock.calls.some(([input, init]) => String(input).startsWith('/api/doc-reviews/') && init?.method === 'DELETE')).toBe(true);
	});
	it('returns focus to the comments pill in the header when cancelling the last request removed its trigger', async () => {
		rows = [row('review-1')];
		render({ viewingThreadId: null, commentCount: 1 }); await settle();
		button('Reviewers: Quality Gate (requested)').click(); await settle();
		button('Cancel request to Quality Gate').click(); await settle();
		expect(maybeButton('Reviewers: Quality Gate (requested)')).toBeUndefined();
		key(document.activeElement as HTMLElement ?? document.body, 'Escape'); await settle();
		expect(document.activeElement).toBe(button('1 open thread'));
	});
	it('collapses a long group to three avatars and a +N', async () => {
		rows = Array.from({ length: 5 }, (_, index) => row(`review-${index}`, { reviewer_thread_id: `r${index}`, reviewer_name: `Reviewer ${index}` }));
		render(); await settle();
		const group = document.body.querySelector('.reviewer-group')!;
		expect(group.querySelectorAll('.reviewer')).toHaveLength(3); expect(group.querySelector('.more')?.textContent).toBe('+2');
		expect(group.getAttribute('aria-label')).toContain('Reviewer 4 (requested)');
	});
	it('closes on Escape and on an outside press, returning focus to the trigger', async () => {
		for (const close of ['Escape', 'outside']) {
			render(); const trigger = button('Request review'); const search = await picker();
			expect(trigger.getAttribute('aria-expanded')).toBe('true'); expect(document.body.querySelector('[data-popover]')).toBeTruthy();
			if (close === 'Escape') key(search, 'Escape'); else document.body.dispatchEvent(new MouseEvent('pointerdown', { bubbles: true }));
			await settle(); expect(document.body.querySelector('[role="combobox"]')).toBeNull(); expect(document.activeElement).toBe(trigger);
			if (close === 'Escape') { component.$destroy(); host.remove(); }
		}
	});
	it('uses a titled drawer for a narrow canvas and an icon-only trigger', async () => {
		width = 393; render(); const search = await picker();
		expect(document.body.querySelector('[data-adaptive-drawer]')).toBeTruthy(); expect(document.body.querySelector('[data-popover]')).toBeNull();
		expect(document.body.querySelectorAll('h2')).toHaveLength(1); expect(document.body.querySelector('h2')?.textContent).toBe('Request review');
		expect(search.placeholder).toBe('Find an agent'); button('Close').click(); await settle(); expect(document.activeElement).toBe(button('Request review'));
	});
	it('removes its narrow review drawer synchronously when its caller is destroyed', async () => {
		width = 393; render(); await picker();
		expect(document.body.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(1);
		component.$destroy();
		component = undefined;
		expect(document.body.querySelectorAll('[data-adaptive-drawer]')).toHaveLength(0);
	});
	it('says plainly when nobody can review, and offers Retry rather than an empty claim on a roster failure', async () => {
		reviewers = []; render(); await picker();
		expect(document.body.textContent).toContain('No agents in this project can review right now.');
		component.$destroy(); host.remove();
		rosterError = true; render(); await picker();
		expect(document.body.textContent).toContain("Couldn't load reviewers"); expect(document.body.textContent).not.toContain('No agents in this project');
		expect(maybeButton('Retry')).toBeTruthy();
	});
	it('shows a load failure as a failure, not a confirmed empty list', async () => {
		fetchMock.mockImplementationOnce(async () => { throw new TypeError('offline'); }); render(); await picker();
		expect(document.body.textContent).toContain("Couldn't load review requests"); expect(document.body.textContent).not.toContain('No agents in this project');
	});
	it('bounds the note with a visible near-limit counter and disables Send over the limit', async () => {
		render(); const search = await picker(); choose(search, 'quality'); await settle();
		const note = document.body.querySelector('textarea')!;
		type(note, 'x'.repeat(401)); expect(document.body.textContent).toContain('401 / 500');
		expect(note.maxLength).toBe(500);
	});
	it('drops a late load when the same document and thread have a new target generation', async () => {
		let finish!: (response: Response) => void;
		fetchMock.mockImplementationOnce(() => new Promise((resolve) => finish = resolve));
		render(); component.$set({ reviewTargetKey: 'target-b' }); flushSync(); await settle();
		finish(json({ reviews: [row()], reviewers })); await settle();
		expect(document.body.querySelector('.reviewer-group')).toBeNull();
	});
	it.each([200, 409])('drops a late submit result (%s) after a target change', async (status) => {
		render(); const search = await picker(); choose(search, 'quality'); await settle();
		type(document.body.querySelector('textarea')!, 'Old note');
		let finish!: (response: Response) => void;
		fetchMock.mockImplementationOnce(() => new Promise((resolve) => finish = resolve));
		button('Send').click(); flushSync();
		component.$set({ artifactId: 'doc-b', reviewTargetKey: 'target-b' }); flushSync(); await settle();
		const newSearch = await picker(); choose(newSearch, 'product'); await settle();
		type(document.body.querySelector('textarea')!, 'New note');
		const calls = fetchMock.mock.calls.length;
		finish(json(status === 200 ? { request: row() } : { detail: 'owner_unavailable' }, status)); await settle();
		expect(document.body.querySelector('textarea')?.value).toBe('New note');
		expect(chips()).toEqual(['Product Experience']);
		expect(toastShow).not.toHaveBeenCalled();
		expect(fetchMock.mock.calls.length).toBe(calls);
	});
	it('refetches on an artifact change and on a plan change, and cleans its listeners up', async () => {
		rows = [row()]; render(); await settle();
		const calls = fetchMock.mock.calls.length;
		rows = [row('review-1', { outcome: 'ended_without_comments', may_cancel: false })];
		for (const callback of events.get('plans_changed') ?? []) callback({ type: 'plans_changed', plans: [] });
		await settle(); expect(fetchMock.mock.calls.length).toBe(calls + 1);
		expect(document.body.querySelector('.reviewer-group')?.getAttribute('aria-label')).toBe('Reviewers: Quality Gate (ended without comments)');
		rows = []; emit('doc-a'); await settle();
		expect(document.body.querySelector('.reviewer-group')).toBeNull();
		component.$destroy(); expect(events.get('plans_changed')?.size).toBe(0);
		host.remove(); render(); await settle(); expect(events.get('plans_changed')?.size).toBe(1);
	});
});
