import { flushSync, mount, unmount } from 'svelte';
import { readable } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import UpdateScreen from './UpdateScreen.svelte';
import { updateScreenView } from '$lib/update/screenStore';

const { reduced } = vi.hoisted(() => ({ reduced: { current: false } }));
vi.mock('$lib/stores/viewport', async (importOriginal) => ({
	...(await importOriginal<typeof import('$lib/stores/viewport')>()),
	prefersReducedMotion: { subscribe: (run: (value: boolean) => void) => readable(reduced.current).subscribe(run) },
}));

type View = Parameters<typeof updateScreenView.set>[0];
const base: NonNullable<View> = { operationId: 'a', mode: 'progress', step: 'starting', label: 'Starting CORSIA', percent: 40, estimated: false, fromVersion: null, toVersion: null, failure: null, serviceDown: false };

let component: Record<string, unknown> | null = null;

function percentText(): string {
	return document.body.querySelector('.percent')?.textContent ?? '';
}

function show(view: NonNullable<View>): void {
	updateScreenView.set(view);
	flushSync();
}

beforeEach(() => {
	vi.useFakeTimers({ toFake: ['requestAnimationFrame', 'cancelAnimationFrame', 'performance'] });
	(window as unknown as Record<string, unknown>).__CORSIA_SHELL__ = { mode: 'local' };
	updateScreenView.set(base);
	component = mount(UpdateScreen, { target: document.body }) as Record<string, unknown>;
	flushSync();
});

afterEach(() => {
	component && unmount(component);
	component = null;
	updateScreenView.set(null);
	reduced.current = false;
	vi.useRealTimers();
	delete (window as unknown as Record<string, unknown>).__CORSIA_SHELL__;
});

describe('UpdateScreen percentage', () => {
	it('shows a whole number and never the word about, estimated or not', () => {
		expect(percentText()).toBe('40%'); // a screen that appears under way starts where it stands
		show({ ...base, percent: 40.9, estimated: true });
		expect(percentText()).not.toMatch(/about|\./i);
	});

	it('counts up to a jump in real progress within a second, never back, and the bar follows', () => {
		show({ ...base, percent: 96 });
		const seen: number[] = [Number.parseInt(percentText(), 10)];
		for (let elapsed = 0; elapsed < 1_000; elapsed += 16) {
			vi.advanceTimersByTime(16);
			flushSync();
			seen.push(Number.parseInt(percentText(), 10));
		}
		expect(seen[0]).toBeLessThan(96);
		expect(seen.at(-1)).toBe(96);
		for (let i = 1; i < seen.length; i += 1) expect(seen[i]).toBeGreaterThanOrEqual(seen[i - 1]);
		expect(document.body.querySelector('[role="progressbar"]')?.getAttribute('aria-valuenow')).toBe('96');
		expect((document.body.querySelector('.bar i') as HTMLElement).style.width).toBe('96%');
	});

	it('starts a replacement at its own progress and counts up without hiding', () => {
		show({ ...base, percent: 80 });
		vi.advanceTimersByTime(1_000);
		flushSync();
		expect(percentText()).toBe('80%');
		const screen = document.querySelector('.update-screen');
		show({ ...base, operationId: 'b', percent: 10 });
		expect(document.querySelector('.update-screen')).toBe(screen);
		expect(percentText()).toBe('10%');
		show({ ...base, operationId: 'b', percent: 25 });
		const seen = [10];
		for (let elapsed = 0; elapsed < 1_000; elapsed += 16) {
			vi.advanceTimersByTime(16);
			flushSync();
			seen.push(Number.parseInt(percentText(), 10));
		}
		expect(seen.at(-1)).toBe(25);
		for (let i = 1; i < seen.length; i += 1) expect(seen[i]).toBeGreaterThanOrEqual(seen[i - 1]);
	});

	it('counts the finish up to 100 the same way', () => {
		show({ ...base, mode: 'done', percent: 99.9, step: 'verifying' });
		vi.advanceTimersByTime(1_000);
		flushSync();
		expect(percentText()).toBe('100%');
	});

	it('steps straight to the value under reduced motion', () => {
		reduced.current = true;
		component && unmount(component);
		updateScreenView.set(null);
		flushSync();
		updateScreenView.set(base);
		component = mount(UpdateScreen, { target: document.body }) as Record<string, unknown>;
		flushSync();
		show({ ...base, percent: 96 });
		expect(percentText()).toBe('96%');
	});
});
