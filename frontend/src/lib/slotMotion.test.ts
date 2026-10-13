import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { blockSlot, inlineSlot } from './slotMotion';

let row: HTMLElement;
let slot: HTMLElement;
let animate: ReturnType<typeof vi.spyOn>;

type FakeAnimation = Animation & { cancel: ReturnType<typeof vi.fn>; finish: () => void };

function fakeAnimation(): FakeAnimation {
	let finish = () => {};
	const finished = new Promise<void>((resolve) => { finish = resolve; });
	return { cancel: vi.fn(), finished, finish } as unknown as FakeAnimation;
}

function currentStyle(element: Element, values: Record<string, string>): ReturnType<typeof vi.spyOn> {
	const original = window.getComputedStyle.bind(window);
	return vi.spyOn(window, 'getComputedStyle').mockImplementation((node) => {
		const style = original(node);
		if (node !== element) return style;
		return new Proxy(style, { get: (target, property) => typeof property === 'string' && property in values ? values[property] : Reflect.get(target, property) }) as CSSStyleDeclaration;
	});
}

beforeEach(() => {
	document.documentElement.style.setProperty('--dur-tight', '120ms');
	document.documentElement.style.setProperty('--dur-standard', '220ms');
	row = document.createElement('div');
	row.style.display = 'flex';
	row.style.columnGap = '4px';
	// The control's own wrapper takes no box: the row's gap is the one that opens and closes.
	const contents = document.createElement('span');
	contents.style.display = 'contents';
	slot = document.createElement('span');
	slot.getBoundingClientRect = () => ({ width: 64 } as DOMRect);
	contents.append(slot);
	row.append(contents);
	document.body.append(row);
	animate = vi.spyOn(Element.prototype, 'animate');
});

afterEach(() => {
	animate.mockRestore();
	row.remove();
	document.documentElement.removeAttribute('style');
});

describe('inlineSlot', () => {
	it('grows a control in where it lands while it fades in, opening the gap with it', () => {
		expect(inlineSlot(slot, undefined, { direction: 'in' })).toEqual({ duration: 220 });
		const [frames, options] = animate.mock.calls[0] as [Keyframe[], KeyframeAnimationOptions];
		expect(frames[0]).toMatchObject({ maxWidth: '0px', minWidth: '0px', marginInlineStart: '-4px', opacity: 0 });
		expect(frames.at(-1)).toMatchObject({ maxWidth: '64px', marginInlineStart: '0px', opacity: 1 });
		expect(options.duration).toBe(220);
	});

	it('fades a leaving control first, then closes its space and the gap', () => {
		expect(inlineSlot(slot, undefined, { direction: 'out' })).toEqual({ duration: 340 });
		const [frames] = animate.mock.calls[0] as [Keyframe[]];
		expect(frames.map((frame) => [frame.opacity, frame.maxWidth])).toEqual([[1, '64px'], [0, '64px'], [0, '0px']]);
		expect(frames[1].offset).toBeCloseTo(120 / 340);
		expect(frames[2]).toMatchObject({ marginInlineStart: '-4px' });
	});

	it('leaves at once, with no motion, when told another control takes its place', () => {
		expect(inlineSlot(slot, { instant: true }, { direction: 'out' })).toEqual({ duration: 0 });
		expect(animate).not.toHaveBeenCalled();
	});

	it('refuses to run both ways, where it could not tell arriving from leaving', () => {
		expect(() => inlineSlot(slot, undefined, { direction: 'both' })).toThrow(/in:inlineSlot out:inlineSlot/);
		expect(animate).not.toHaveBeenCalled();
	});

	it('only fades with reduced motion', () => {
		const matchMedia = vi.spyOn(window, 'matchMedia').mockImplementation((query) => ({ matches: query.includes('reduce'), media: query, addEventListener() {}, removeEventListener() {} }) as unknown as MediaQueryList);
		try {
			expect(inlineSlot(slot, undefined, { direction: 'out' })).toEqual({ duration: 120 });
			const [frames] = animate.mock.calls[0] as [Keyframe[]];
			expect(frames).toEqual([{ opacity: 1 }, { opacity: 0 }]);
		} finally {
			matchMedia.mockRestore();
		}
	});

	it('cancels a filling exit and settles a reversed entry at its rest width', async () => {
		const exit = fakeAnimation();
		const entry = fakeAnimation();
		animate.mockImplementationOnce(() => exit).mockImplementationOnce(() => entry);
		inlineSlot(slot, undefined, { direction: 'out' });
		const style = currentStyle(slot, { maxWidth: '32px', minWidth: '0px', marginInlineStart: '-2px', opacity: '0.5' });
		try {
			expect(inlineSlot(slot, undefined, { direction: 'in' })).toEqual({ duration: 110 });
			const [frames, options] = animate.mock.calls[1] as [Keyframe[], KeyframeAnimationOptions];
			expect(exit.cancel).toHaveBeenCalledOnce();
			expect(frames[0]).toMatchObject({ maxWidth: '32px', opacity: '0.5' });
			expect(frames.at(-1)).toMatchObject({ maxWidth: '64px', opacity: 1 });
			expect(options.fill).toBeUndefined();
			entry.finish();
			await Promise.resolve();
			expect(slot.style.overflow).toBe('');
		} finally {
			style.mockRestore();
		}
	});

	it('begins a reversed exit at the current width', () => {
		const entry = fakeAnimation();
		const exit = fakeAnimation();
		animate.mockImplementationOnce(() => entry).mockImplementationOnce(() => exit);
		inlineSlot(slot, undefined, { direction: 'in' });
		const style = currentStyle(slot, { maxWidth: '32px', minWidth: '0px', marginInlineStart: '-2px', opacity: '0.5' });
		try {
			expect(inlineSlot(slot, undefined, { direction: 'out' })).toEqual({ duration: 170 });
			const [frames] = animate.mock.calls[1] as [Keyframe[]];
			expect(entry.cancel).toHaveBeenCalledOnce();
			expect(frames[0]).toMatchObject({ maxWidth: '32px', opacity: '0.5' });
		} finally {
			style.mockRestore();
		}
	});
});
