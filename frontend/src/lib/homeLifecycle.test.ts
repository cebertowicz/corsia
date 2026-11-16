import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';

import { onSheetMoved } from './docCommentDrawer';
import { registerHomeLifecycle } from './homeLifecycle';

const stubPointer = (coarse: boolean) => vi.stubGlobal('matchMedia', (q: string) => ({ matches: coarse && q.includes('coarse'), media: q }));

describe('keyboard inset only where a soft keyboard can open', () => {
	let teardown: () => void;
	let input: HTMLTextAreaElement;
	const style = () => document.documentElement.style;
	const props = ['--kb-toolbar-inset', '--floater-bottom-pad', '--messages-pad-top'];
	const setup = (coarse: boolean) => {
		stubPointer(coarse);
		input = document.createElement('textarea');
		document.body.append(input);
		teardown = registerHomeLifecycle({
			token: () => null, activeThreadId: () => null, activeView: () => 'chat', threads: () => [],
			loadThreads: vi.fn(), loadModels: vi.fn(), loadSettings: vi.fn(), loadUpdateStatus: vi.fn(),
			pollSystem: vi.fn(), checkSecurityAlert: vi.fn(), selectThread: vi.fn(async () => {}),
			setActiveView: vi.fn(), chatPanel: () => undefined, on: () => () => {}, connect: vi.fn(),
		});
	};
	afterEach(() => {
		teardown();
		input.remove();
		props.forEach((p) => style().removeProperty(p));
		vi.unstubAllGlobals();
	});

	it('sets the inset on focus with a coarse pointer', () => {
		setup(true);
		input.focus();
		expect(style().getPropertyValue('--kb-toolbar-inset')).toBe('60px');
		expect(style().getPropertyValue('--floater-bottom-pad')).toBe('4px');
		expect(style().getPropertyValue('--messages-pad-top')).not.toBe('');
	});

	it('leaves the inset and paddings untouched on focus with a fine pointer', () => {
		setup(false);
		input.focus();
		props.forEach((p) => expect(style().getPropertyValue(p)).toBe(''));
	});
});

describe('keyboard inset focus ordering', () => {
	let teardown: () => void;
	let input: HTMLTextAreaElement;
	let button: HTMLButtonElement;

	beforeEach(() => {
		stubPointer(true);
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
		vi.unstubAllGlobals();
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

	it('keeps the transcript header clearance while an editor in a sheet has focus', () => {
		const lead = () => document.documentElement.style.getPropertyValue('--messages-pad-top');
		const sheet = document.createElement('div');
		sheet.setAttribute('data-adaptive-drawer', '');
		const sheetEditor = document.createElement('textarea');
		sheet.append(sheetEditor);
		document.body.append(sheet);
		input.focus();
		expect(lead()).not.toBe('');
		sheetEditor.focus();
		expect(lead()).toBe('');
		expect(inset()).toBe('60px');
		sheet.remove();
	});
});

describe('transcript offset around a focused editor', () => {
	const setup = (commentSheet: boolean) => {
		stubPointer(true);
		vi.useFakeTimers();
		vi.stubGlobal('requestAnimationFrame', (cb: FrameRequestCallback) => setTimeout(() => cb(0), 0) as unknown as number);
		const panel = { captureBottomOffset: vi.fn(), restoreBottomOffset: vi.fn() };
		const teardown = registerHomeLifecycle({
			token: () => null, activeThreadId: () => null, activeView: () => 'chat', threads: () => [],
			loadThreads: vi.fn(), loadModels: vi.fn(), loadSettings: vi.fn(), loadUpdateStatus: vi.fn(),
			pollSystem: vi.fn(), checkSecurityAlert: vi.fn(), selectThread: vi.fn(async () => {}),
			setActiveView: vi.fn(), chatPanel: () => panel, on: () => () => {}, connect: vi.fn(),
		} as unknown as Parameters<typeof registerHomeLifecycle>[0]);
		const composer = document.createElement('textarea');
		const sheet = document.createElement('div');
		sheet.setAttribute('data-adaptive-drawer', '');
		if (commentSheet) sheet.setAttribute('data-keeps-text', '');
		const reply = document.createElement('textarea');
		sheet.append(reply);
		document.body.append(composer);
		composer.focus();
		vi.advanceTimersByTime(10);
		expect(panel.restoreBottomOffset).toHaveBeenCalledTimes(1);
		panel.captureBottomOffset.mockClear();
		panel.restoreBottomOffset.mockClear();
		document.body.append(sheet);
		const done = () => { teardown(); composer.remove(); sheet.remove(); vi.clearAllTimers(); vi.unstubAllGlobals(); vi.useRealTimers(); };
		return { panel, sheet, reply, done };
	};

	it('does not put the transcript back for an editor in a comment sheet, which places its passage above itself', () => {
		const { panel, sheet, reply, done } = setup(true);
		const reveal = vi.fn();
		const stop = onSheetMoved(sheet, reveal);
		vi.advanceTimersByTime(10);
		reveal.mockClear();
		reply.focus();
		vi.advanceTimersByTime(10);
		expect(panel.restoreBottomOffset).not.toHaveBeenCalled();
		expect(reveal).toHaveBeenCalledWith(false);
		stop();
		done();
	});

	it('still puts the transcript back for an editor in another kind of drawer (the attention dock, a plan drawer)', () => {
		const { panel, reply, done } = setup(false);
		reply.focus();
		vi.advanceTimersByTime(10);
		expect(panel.restoreBottomOffset).toHaveBeenCalledTimes(1);
		done();
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
