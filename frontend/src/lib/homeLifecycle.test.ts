import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { registerHomeLifecycle } from './homeLifecycle';

describe('keyboard inset focus ordering', () => {
	let teardown: () => void;
	let input: HTMLTextAreaElement;
	let button: HTMLButtonElement;

	beforeEach(() => {
		vi.useFakeTimers();
		input = document.createElement('textarea');
		button = document.createElement('button');
		document.body.append(input, button);
		teardown = registerHomeLifecycle({
			token: () => null, activeThreadId: () => null, activeView: () => 'chat', threads: () => [],
			loadThreads: vi.fn(), loadModels: vi.fn(), loadSettings: vi.fn(), loadUpdateStatus: vi.fn(),
			pollSystem: vi.fn(), checkSecurityAlert: vi.fn(), selectThread: vi.fn(async () => {}),
			setActiveView: vi.fn(), chatPanel: () => undefined, on: () => () => {}, connect: vi.fn(),
		});
	});

	afterEach(() => {
		teardown();
		input.remove();
		button.remove();
		document.documentElement.style.removeProperty('--kb-toolbar-inset');
		vi.clearAllTimers();
		vi.useRealTimers();
	});

	const pointer = (target: HTMLElement, type: string) => {
		const event = new Event(type, { bubbles: true });
		Object.defineProperty(event, 'pointerId', { value: 1 });
		target.dispatchEvent(event);
	};
	const inset = () => document.documentElement.style.getPropertyValue('--kb-toolbar-inset');

	it('keeps a non-chat button stable through a touch gesture after the pointer timer fires', () => {
		input.focus();
		pointer(button, 'pointerdown');
		pointer(button, 'pointerup');
		vi.advanceTimersByTime(0);
		button.dispatchEvent(new MouseEvent('mousedown', { bubbles: true }));
		input.blur();
		expect(inset()).toBe('60px');
		button.dispatchEvent(new MouseEvent('mouseup', { bubbles: true }));
		button.addEventListener('click', () => expect(inset()).toBe('60px'));
		button.click();
		vi.advanceTimersByTime(0);
		expect(inset()).toBe('0px');
	});

	it('keeps the inset through pointerup and click before clearing it', () => {
		input.focus();
		pointer(button, 'pointerdown');
		input.blur();
		expect(inset()).toBe('60px');
		pointer(button, 'pointerup');
		button.addEventListener('click', () => expect(inset()).toBe('60px'));
		button.click();
		expect(inset()).toBe('60px');
		vi.runAllTimers();
		expect(inset()).toBe('0px');
	});

	it('also keeps the inset if focusout follows pointerup before click', () => {
		input.focus();
		pointer(button, 'pointerdown');
		pointer(button, 'pointerup');
		input.blur();
		button.addEventListener('click', () => expect(inset()).toBe('60px'));
		button.click();
		vi.runAllTimers();
		expect(inset()).toBe('0px');
	});

	it('clears after cancellation or blur without a press', () => {
		input.focus();
		pointer(button, 'pointerdown');
		input.blur();
		pointer(button, 'pointercancel');
		vi.advanceTimersByTime(0);
		expect(inset()).toBe('0px');
		input.focus();
		input.blur();
		vi.advanceTimersByTime(0);
		expect(inset()).toBe('0px');
	});

	it('keeps the inset if another editable receives focus before the deferred clear', () => {
		input.focus();
		pointer(button, 'pointerdown');
		input.blur();
		pointer(button, 'pointerup');
		input.focus();
		vi.runAllTimers();
		expect(inset()).toBe('60px');
	});
});

describe('registerHomeLifecycle polling', () => {
	beforeEach(() => {
		vi.useFakeTimers();
	});

	afterEach(() => {
		vi.clearAllTimers();
		vi.useRealTimers();
	});

	it('keeps one periodic timer across reconnects while refreshing every connection', () => {
		const listeners = new Map<string, (data: any) => void>();
		const loadThreads = vi.fn();
		const loadModels = vi.fn();
		const loadSettings = vi.fn();
		const loadUpdateStatus = vi.fn();
		const pollSystem = vi.fn();
		const checkSecurityAlert = vi.fn();
		const teardown = registerHomeLifecycle({
			token: () => null,
			activeThreadId: () => null,
			activeView: () => 'activity',
			threads: () => [],
			loadThreads,
			loadModels,
			loadSettings,
			loadUpdateStatus,
			pollSystem,
			checkSecurityAlert,
			selectThread: vi.fn(async () => {}),
			setActiveView: vi.fn(),
			chatPanel: () => undefined,
			on: (event, callback) => {
				listeners.set(event, callback);
				return () => listeners.delete(event);
			},
			connect: vi.fn(),
		});

		listeners.get('connected')?.(undefined);
		listeners.get('connected')?.(undefined);

		expect(loadThreads).toHaveBeenCalledTimes(2);
		expect(loadModels).toHaveBeenCalledTimes(2);
		expect(loadSettings).toHaveBeenCalledTimes(2);
		expect(loadUpdateStatus).toHaveBeenCalledTimes(2);
		expect(pollSystem).toHaveBeenCalledTimes(2);
		expect(checkSecurityAlert).toHaveBeenCalledTimes(2);
		expect(vi.getTimerCount()).toBe(1);

		vi.advanceTimersByTime(30_000);

		expect(loadSettings).toHaveBeenCalledTimes(3);
		expect(loadUpdateStatus).toHaveBeenCalledTimes(3);
		expect(pollSystem).toHaveBeenCalledTimes(3);
		expect(loadThreads).toHaveBeenCalledTimes(2);
		expect(loadModels).toHaveBeenCalledTimes(2);
		expect(checkSecurityAlert).toHaveBeenCalledTimes(2);

		teardown();
		expect(vi.getTimerCount()).toBe(0);
	});
});
