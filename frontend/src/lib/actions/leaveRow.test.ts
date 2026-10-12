import { describe, expect, it, vi } from 'vitest';
import { leaveRow } from './leaveRow';

function row() {
	const node = document.createElement('div');
	document.body.append(node);
	const animate = vi.fn(() => ({ cancel: vi.fn() }) as unknown as Animation);
	node.animate = animate as unknown as typeof node.animate;
	return { node, animate };
}

describe('leaveRow', () => {
	it('does nothing until the row is leaving, then collapses and fades it once', () => {
		const { node, animate } = row();
		const action = leaveRow(node, { leaving: false });
		expect(animate).not.toHaveBeenCalled();
		action.update({ leaving: true });
		action.update({ leaving: true });
		expect(animate).toHaveBeenCalledTimes(1);
		const frames = (animate.mock.calls[0] as unknown as [Keyframe[]])[0];
		expect(frames[0]).toMatchObject({ opacity: 1 });
		expect(frames[1]).toMatchObject({ height: '0px', paddingTop: '0px', paddingBottom: '0px', opacity: 0 });
		expect(node.style.overflow).toBe('hidden');
	});

	it('only fades under reduced motion: nothing slides', () => {
		const { node, animate } = row();
		leaveRow(node, { leaving: true, reduced: true });
		const frames = (animate.mock.calls[0] as unknown as [Keyframe[]])[0];
		expect(frames).toEqual([{ opacity: 1 }, { opacity: 0 }]);
		expect(node.style.overflow).toBe('');
	});
});
