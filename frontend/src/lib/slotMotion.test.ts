import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { inlineSlot } from './slotMotion';

let row: HTMLElement;
let slot: HTMLElement;
let animate: ReturnType<typeof vi.spyOn>;

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
});
