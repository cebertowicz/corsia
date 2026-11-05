import { afterEach, describe, expect, it, vi } from 'vitest';
import { clearTabDocumentImage, clearTabViewState, openTabDocumentImage, tabDocumentImage } from './tabViewState';

const image = (src: string) => ({ src, alt: 'Diagram', from: 1, to: 2, scrollTop: 0, top: 0 });

describe('document image snapshots', () => {
	afterEach(() => { clearTabViewState('thread', 'one'); clearTabViewState('thread', 'two'); vi.restoreAllMocks(); });
	it('keeps inactive tabs alive and releases a snapshot on replacement, Back and close', () => {
		const revoke = vi.spyOn(URL, 'revokeObjectURL').mockImplementation(() => {});
		openTabDocumentImage('thread', 'one', image('blob:one'));
		openTabDocumentImage('thread', 'two', image('blob:two'));
		expect(tabDocumentImage('thread', 'one')?.src).toBe('blob:one');
		expect(revoke).not.toHaveBeenCalled();
		openTabDocumentImage('thread', 'one', image('blob:replacement'));
		expect(revoke).toHaveBeenCalledWith('blob:one');
		clearTabDocumentImage('thread', 'one');
		expect(revoke).toHaveBeenCalledWith('blob:replacement');
		expect(tabDocumentImage('thread', 'two')?.src).toBe('blob:two');
		clearTabViewState('thread', 'two');
		expect(revoke).toHaveBeenCalledWith('blob:two');
		expect(revoke).toHaveBeenCalledTimes(3);
	});
});
