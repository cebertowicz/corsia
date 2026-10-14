import { get } from 'svelte/store';
import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { toasts } from '$lib/stores/toast';
import ToastContainer from './ToastContainer.svelte';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

function clearToasts() {
	for (const toast of get(toasts)) toasts.dismiss(toast.id);
}

beforeEach(() => {
	vi.useFakeTimers();
	clearToasts();
	host = document.createElement('div');
	document.body.appendChild(host);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	clearToasts();
	host.remove();
	vi.useRealTimers();
});

function showActionToast() {
	(toasts as any).showAction('Removed from pane', { label: 'Undo', run: vi.fn() });
	component = mount(ToastContainer, { target: host });
}

describe('ToastContainer action', () => {
	it('renders the visible action label on a real button', () => {
		showActionToast();
		const action = [...host.querySelectorAll('button')].find(
			(button) => button.textContent?.trim() === 'Undo',
		);
		expect(action).toBeTruthy();
		expect(action?.getAttribute('aria-label') ?? action?.textContent?.trim()).toBe('Undo');
	});

	it('pauses auto-dismiss while the pointer is inside', async () => {
		showActionToast();
		const toast = host.querySelector<HTMLElement>('.toast')!;
		await vi.advanceTimersByTimeAsync(7000);
		toast.dispatchEvent(new MouseEvent('mouseenter'));
		await vi.advanceTimersByTimeAsync(2000);
		expect(get(toasts)).toHaveLength(1);
		toast.dispatchEvent(new MouseEvent('mouseleave'));
		await vi.advanceTimersByTimeAsync(1001);
		expect(get(toasts)).toHaveLength(0);
	});

	it('stays paused until both pointer and focus leave', async () => {
		showActionToast();
		const toast = host.querySelector<HTMLElement>('.toast')!;
		const action = [...host.querySelectorAll<HTMLButtonElement>('button')].find(
			(button) => button.textContent?.trim() === 'Undo',
		)!;
		await vi.advanceTimersByTimeAsync(7000);
		toast.dispatchEvent(new MouseEvent('mouseenter'));
		action.focus();
		toast.dispatchEvent(new MouseEvent('mouseleave'));
		await vi.advanceTimersByTimeAsync(2000);
		expect(get(toasts)).toHaveLength(1);
		action.blur();
		await vi.advanceTimersByTimeAsync(1001);
		expect(get(toasts)).toHaveLength(0);
	});

	it('pauses auto-dismiss while keyboard focus is inside', async () => {
		showActionToast();
		const action = [...host.querySelectorAll<HTMLButtonElement>('button')].find(
			(button) => button.textContent?.trim() === 'Undo',
		)!;
		await vi.advanceTimersByTimeAsync(7000);
		action.focus();
		await vi.advanceTimersByTimeAsync(2000);
		expect(get(toasts)).toHaveLength(1);
		action.blur();
		await vi.advanceTimersByTimeAsync(1001);
		expect(get(toasts)).toHaveLength(0);
	});
});

function mountContainer() {
	component = mount(ToastContainer, { target: host });
	return host.querySelector<HTMLElement>('.toast-container')!;
}

describe('ToastContainer pinned slot', () => {
	it('shows a spinner, the caller close label and the action on progress', () => {
		toasts.pin('update', {
			type: 'progress',
			message: 'Stopping agents · 2 of 3 safe',
			time: '0:18',
			closeLabel: 'Hide progress',
			action: { label: 'Details', run: vi.fn() },
		});
		mountContainer();

		expect(host.querySelector('.toast-spinner')).toBeTruthy();
		const close = host.querySelector<HTMLButtonElement>('.toast-close')!;
		expect(close.getAttribute('aria-label')).toBe('Hide progress');
		expect(close.title).toBe('Hide progress');
		expect(host.querySelector('.toast-action')?.textContent?.trim()).toBe('Details');
	});

	it('keeps the ticking time out of the live region', () => {
		toasts.pin('update', {
			type: 'progress',
			message: 'Stopping agents · 2 of 3 safe',
			time: '0:18',
		});
		mountContainer();

		const status = host.querySelector('[role="status"]')!;
		expect(status.textContent).toBe('Stopping agents · 2 of 3 safe');
		const time = host.querySelector('.toast-time')!;
		expect(time.getAttribute('aria-hidden')).toBe('true');
		expect(time.textContent).toBe('0:18');
		expect(status.contains(time)).toBe(false);
	});

	it('announces only the new step when the time ticks and the step changes', async () => {
		toasts.pin('update', { type: 'progress', message: 'Stopping agents', time: '0:01' });
		mountContainer();
		const status = host.querySelector('[role="status"]')!;
		toasts.pin('update', { type: 'progress', message: 'Stopping agents', time: '0:02' });
		await tick();
		expect(host.querySelector('[role="status"]')).toBe(status);
		expect(status.textContent).toBe('Stopping agents');
		toasts.pin('update', { type: 'progress', message: 'Restarting', time: '0:03' });
		await tick();
		expect(host.querySelector('[role="status"]')).toBe(status);
		expect(status.textContent).toBe('Restarting');
	});

	it('shows an error as an alert with a leading icon and its Details action', () => {
		toasts.pin('update', {
			type: 'error',
			message: 'Update stopped',
			action: { label: 'Details', run: vi.fn() },
		});
		mountContainer();

		const toast = host.querySelector('.toast')!;
		expect(toast.getAttribute('role')).toBe('alert');
		expect(toast.querySelector('svg.toast-icon')).toBeTruthy();
		expect(toast.querySelector('.toast-action')?.textContent?.trim()).toBe('Details');
		expect(toast.querySelector('.toast-close')?.getAttribute('aria-label')).toBe('Dismiss');
	});

	it('leads every pinned state with a glyph, and an ordinary info toast with none', async () => {
		toasts.pin('update', { type: 'success', message: 'Updated' });
		mountContainer();
		expect(host.querySelector('.toast--success svg.toast-icon')).toBeTruthy();

		toasts.pin('update', { type: 'info', message: 'An update is ready' });
		await tick();
		expect(host.querySelectorAll('.toast')).toHaveLength(1);
		expect(host.querySelector('.toast--pinned.toast--info svg.toast-icon--update')).toBeTruthy();

		toasts.show('Saved');
		await tick();
		expect(host.querySelector('.toast:not(.toast--pinned) .toast-icon')).toBeNull();
	});

	it('renders the one ordinary toast above the pinned one, replacing in place', async () => {
		toasts.pin('update', { type: 'progress', message: 'Restarting' });
		toasts.show('Archived');
		mountContainer();
		expect([...host.querySelectorAll('.toast-text')].map((el) => el.textContent)).toEqual([
			'Archived',
			'Restarting',
		]);

		toasts.show('Moved');
		await tick();
		await vi.advanceTimersByTimeAsync(300);
		expect([...host.querySelectorAll('.toast-text')].map((el) => el.textContent)).toEqual([
			'Moved',
			'Restarting',
		]);
	});

	it('uses the shipped role on ordinary toasts', () => {
		toasts.show('Saved');
		mountContainer();
		expect(host.querySelector('.toast')?.getAttribute('role')).toBe('status');
	});
});

describe('ToastContainer fit', () => {
	// jsdom has no layout; model a row that overflows until enough has been dropped.
	let overflowsAt: (fit: string) => boolean;

	beforeEach(() => {
		overflowsAt = () => false;
		Object.defineProperty(HTMLElement.prototype, 'scrollWidth', {
			configurable: true,
			get(this: HTMLElement) {
				return this.classList.contains('toast--pinned') &&
					overflowsAt(this.dataset.fit ?? '')
					? 500
					: 100;
			},
		});
		Object.defineProperty(HTMLElement.prototype, 'clientWidth', {
			configurable: true,
			get: () => 300,
		});
	});

	afterEach(() => {
		delete (HTMLElement.prototype as unknown as Record<string, unknown>).scrollWidth;
		delete (HTMLElement.prototype as unknown as Record<string, unknown>).clientWidth;
	});

	function pinProgress(time = '0:18') {
		toasts.pin('update', { type: 'progress', message: 'Stopping agents · 2 of 3 safe', time });
	}

	it('keeps the whole line when it fits', async () => {
		pinProgress();
		mountContainer();
		await tick();
		expect(host.querySelector<HTMLElement>('.toast')!.dataset.fit).toBe('full');
	});

	it('drops the time first when the line would overflow', async () => {
		overflowsAt = (fit) => fit === 'full';
		pinProgress();
		mountContainer();
		await tick();
		expect(host.querySelector<HTMLElement>('.toast')!.dataset.fit).toBe('no-time');
	});

	it('wraps the step text to two lines only when it still does not fit', async () => {
		overflowsAt = (fit) => fit !== 'wrap';
		pinProgress();
		mountContainer();
		await tick();
		expect(host.querySelector<HTMLElement>('.toast')!.dataset.fit).toBe('wrap');
	});

	it('re-settles by measured fit when the content changes, not by breakpoint', async () => {
		pinProgress('0:09');
		mountContainer();
		await tick();
		const toast = host.querySelector<HTMLElement>('.toast')!;
		expect(toast.dataset.fit).toBe('full');

		overflowsAt = (fit) => fit === 'full';
		pinProgress('10:00');
		await tick();
		expect(toast.dataset.fit).toBe('no-time');

		overflowsAt = () => false;
		pinProgress('10:01');
		await tick();
		expect(toast.dataset.fit).toBe('full');
	});

	it('renders a pinned failure detail as a second line', () => {
		toasts.pin('update', { type: 'error', message: "Update didn't start", detail: 'Needs 12 GB more free space' });
		mountContainer();
		expect(host.querySelector('.toast-detail')?.textContent).toBe('Needs 12 GB more free space');
	});

	it('never measures an ordinary toast', async () => {
		overflowsAt = () => true;
		toasts.show('A long ordinary toast');
		mountContainer();
		await tick();
		expect(host.querySelector<HTMLElement>('.toast')!.dataset.fit).toBe('full');
	});
});

describe('ToastContainer leave', () => {
	let leaveOptions: KeyframeAnimationOptions | undefined;

	async function leaveKeyframes(reduced: boolean): Promise<Record<string, unknown>[]> {
		const original = window.matchMedia;
		window.matchMedia = ((query: string) => ({
			matches: reduced && query.includes('prefers-reduced-motion'),
			media: query,
			addEventListener() {},
			removeEventListener() {},
			addListener() {},
			removeListener() {},
			dispatchEvent: () => false,
		})) as unknown as typeof window.matchMedia;
		const animate = vi.spyOn(Element.prototype, 'animate');
		try {
			toasts.show('Leaving');
			component = mount(ToastContainer, { target: host });
			flushSync();
			animate.mockClear();
			toasts.dismiss(get(toasts)[0].id);
			flushSync();
			await vi.advanceTimersByTimeAsync(10);
			leaveOptions = animate.mock.calls.map(([, opts]) => opts as KeyframeAnimationOptions).find((opts) => opts?.fill === 'forwards');
			return animate.mock.calls.flatMap(([frames]) => frames as Record<string, unknown>[]);
		} finally {
			animate.mockRestore();
			window.matchMedia = original;
		}
	}

	it('leaves at 70% of the standard duration on the snap easing, both read from the tokens', async () => {
		const root = document.documentElement.style;
		root.setProperty('--dur-standard', '200ms');
		root.setProperty('--ease-snap', 'cubic-bezier(0.2, 0, 0, 1)');
		try {
			await leaveKeyframes(false);
			expect(leaveOptions?.duration).toBe(140);
			expect(leaveOptions?.easing).toBe('cubic-bezier(0.2, 0, 0, 1)');
		} finally {
			root.removeProperty('--dur-standard');
			root.removeProperty('--ease-snap');
		}
	});

	it('fades and drops a leaving toast, the last one included', async () => {
		const frames = await leaveKeyframes(false);
		expect(frames.length).toBeGreaterThan(1);
		expect(frames.some((frame) => 'opacity' in frame)).toBe(true);
		expect(frames.some((frame) => String(frame.transform ?? '').includes('translateY'))).toBe(true);
	});

	it('only fades under reduced motion', async () => {
		const frames = await leaveKeyframes(true);
		expect(frames.some((frame) => 'opacity' in frame)).toBe(true);
		expect(frames.some((frame) => 'transform' in frame)).toBe(false);
	});
});
