import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
const { prefersReducedMotion } = vi.hoisted(() => ({ prefersReducedMotion: { current: false } }));
vi.mock('$lib/stores/viewport', async () => {
	const { readable } = await import('svelte/store');
	return { prefersReducedMotion: { subscribe: (run: (value: boolean) => void) => readable(prefersReducedMotion.current).subscribe(run) } };
});
import { menuPop, peerFade } from './popoverMotion';

const animate = vi.fn();

beforeEach(() => {
	animate.mockReset();
	document.documentElement.style.setProperty('--dur-tight', '120ms');
	document.documentElement.style.setProperty('--ease-snap', 'cubic-bezier(0.2, 0, 0, 1)');
	prefersReducedMotion.current = false;
});
afterEach(() => { prefersReducedMotion.current = false; });

function node() {
	const element = document.createElement('div');
	element.animate = animate as unknown as typeof element.animate;
	return element;
}

describe('menuPop', () => {
	it('fades a menu in from its anchor over --dur-tight and leaves at 70% of it', () => {
		const entering = menuPop(node(), { origin: 'top right' }, { direction: 'in' });
		expect(entering.duration).toBe(120);
		expect(animate.mock.calls[0][0]).toEqual([{ opacity: 0, transform: 'translateY(-4px) scale(0.98)' }, { opacity: 1, transform: 'none' }]);
		expect(animate.mock.calls[0][1]).toMatchObject({ duration: 120, easing: 'cubic-bezier(0.2, 0, 0, 1)' });
		const leaving = menuPop(node(), undefined, { direction: 'out' });
		expect(leaving.duration).toBeCloseTo(84);
		expect(animate.mock.calls[1][0]).toEqual([{ opacity: 1, transform: 'none' }, { opacity: 0, transform: 'translateY(-4px) scale(0.98)' }]);
		expect(animate.mock.calls[1][1]).toMatchObject({ fill: 'forwards' });
	});

	it('only fades under reduced motion', () => {
		prefersReducedMotion.current = true;
		menuPop(node(), undefined, { direction: 'in' });
		expect(animate.mock.calls[0][0]).toEqual([{ opacity: 0 }, { opacity: 1, transform: 'none' }]);
	});
});

describe('peerFade', () => {
	it('crossfades over --dur-tight with the exit at 70%', () => {
		expect(peerFade(node(), undefined, { direction: 'in' }).duration).toBe(120);
		expect(peerFade(node(), undefined, { direction: 'out' }).duration).toBeCloseTo(84);
		expect(animate.mock.calls[0][0]).toEqual([{ opacity: 0 }, { opacity: 1 }]);
	});
});
