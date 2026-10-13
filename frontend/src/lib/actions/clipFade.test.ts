import { beforeEach, describe, expect, it, vi } from 'vitest';

const reduced = vi.hoisted(() => ({ store: null as unknown as import('svelte/store').Writable<boolean> }));
vi.mock('$lib/stores/viewport', async () => {
	const { writable: make } = await import('svelte/store');
	reduced.store = make(false);
	return { prefersReducedMotion: reduced.store };
});

import { clipFade } from './clipFade';

describe('clipFade', () => {
	beforeEach(() => { reduced.store.set(false); });

	it('eases the size and the opacity together', () => {
		const node = document.createElement('div');
		document.body.append(node);
		const config = clipFade(node, { axis: 'y' });
		const css = (config as { css: (t: number, u: number) => string }).css(0.5, 0.5);
		expect(css).toContain('height');
		expect(css).toMatch(/opacity: 0\.5$/);
	});

	it('only fades under reduced motion: nothing slides', () => {
		reduced.store.set(true);
		const node = document.createElement('div');
		document.body.append(node);
		const css = (clipFade(node) as { css: (t: number, u: number) => string }).css(0.5, 0.5);
		expect(css).toBe('opacity: 0.5');
	});
});
