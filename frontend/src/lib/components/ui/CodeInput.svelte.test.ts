import { flushSync, mount, tick, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import CodeInput from './CodeInput.svelte';

// Several waits here ride real timers (a one-second poll, a 200 ms hold); a loaded machine must not fail them.
const waitFor = <T,>(fn: () => T | Promise<T>, options: { timeout?: number } = {}) => vi.waitFor(fn, { timeout: 4000, ...options });

let host: HTMLDivElement;
let component: ReturnType<typeof mount> | null = null;
const state = $state({ value: '', refocus: 0 });
const boxes = () => Array.from(host.querySelectorAll<HTMLInputElement>('input'));
const type = (box: HTMLInputElement, text: string) => { box.value = text; box.dispatchEvent(new Event('input', { bubbles: true })); flushSync(); };

function show(props: Record<string, unknown> = {}) {
	component = mount(CodeInput, { target: host, props: { label: 'Code', onsubmit: props.onsubmit ?? vi.fn(), ...props } });
	flushSync();
}
beforeEach(() => { host = document.createElement('div'); document.body.appendChild(host); });
afterEach(() => { if (component) void unmount(component); component = null; host.remove(); });

describe('CodeInput', () => {
	it('has four boxes, each named by its place', () => {
		show();
		expect(boxes().map((box) => box.getAttribute('aria-label'))).toEqual(['Code, character 1 of 4', 'Code, character 2 of 4', 'Code, character 3 of 4', 'Code, character 4 of 4']);
	});
	it('moves to the next box as a character is typed, upper-cased, and submits on the fourth', () => {
		const onsubmit = vi.fn();
		show({ onsubmit, focusAt: undefined });
		boxes().forEach((box, i) => { box.focus(); type(box, 'k7qx'[i]); });
		expect(boxes().map((box) => box.value).join('')).toBe('K7QX');
		expect(onsubmit).toHaveBeenCalledWith('K7QX');
	});
	it('fills every box from a paste', () => {
		const onsubmit = vi.fn();
		show({ onsubmit });
		boxes()[0].dispatchEvent(Object.assign(new Event('paste', { bubbles: true, cancelable: true }), { clipboardData: { getData: () => ' k7qx ' } }));
		flushSync();
		expect(boxes().map((box) => box.value).join('')).toBe('K7QX');
		expect(onsubmit).toHaveBeenCalledWith('K7QX');
	});
	it('backspace in an empty box goes back to the previous one', () => {
		show({ onsubmit: vi.fn() });
		boxes()[0].focus(); type(boxes()[0], 'K');
		expect(document.activeElement).toBe(boxes()[1]);
		boxes()[1].dispatchEvent(new KeyboardEvent('keydown', { key: 'Backspace', bubbles: true }));
		flushSync();
		expect(document.activeElement).toBe(boxes()[0]);
	});
	it('takes focus in the first box when it shows', async () => {
		show({ focusAt: 0 });
		await waitFor(() => expect(document.activeElement).toBe(boxes()[0]));
	});
	it('a disabled input accepts nothing', () => {
		show({ disabled: true });
		expect(boxes().every((box) => box.disabled)).toBe(true);
	});
	it('refocus returns the cursor to the first empty box after the caller clears the code', async () => {
		component = mount(CodeInput, { target: host, props: {
			label: 'Code', onsubmit: vi.fn(),
			get value() { return state.value; }, set value(next: string) { state.value = next; },
			get refocus() { return state.refocus; },
		} });
		flushSync();
		boxes()[3].focus();
		state.value = ''; state.refocus += 1;
		await tick();
		expect(document.activeElement).toBe(boxes()[0]);
	});
});
