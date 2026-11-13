// @vitest-environment jsdom
import { afterEach, describe, expect, it, vi } from 'vitest';
import { keepInView } from './keepInView';

/** A scroller holding a field and, below it, a sticky row pinned to the scroller's bottom. */
function build(pinnedHeight: number | null) {
	const owner = document.createElement('div');
	owner.style.overflowY = 'auto';
	const field = document.createElement('textarea');
	owner.append(field);
	if (pinnedHeight !== null) {
		const pin = document.createElement('div');
		pin.setAttribute('data-pin-bottom', '');
		pin.style.position = 'sticky';
		Object.defineProperty(pin, 'offsetHeight', { value: pinnedHeight });
		owner.append(pin);
	}
	document.body.append(owner);
	return { owner, field };
}

afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); });

describe('keepInView with a pinned bottom row', () => {
	it('leaves the height of a sticky [data-pin-bottom] row clear below the field it reveals', () => {
		const { field } = build(52);
		const action = keepInView(field, { enabled: true });
		action.update({ enabled: true });
		expect(field.style.scrollMarginBottom).toBe('52px');
		action.destroy();
	});

	it('asks for no margin when nothing is pinned', () => {
		const { field } = build(null);
		const action = keepInView(field, { enabled: true });
		action.update({ enabled: true });
		expect(field.style.scrollMarginBottom).toBe('0px');
		action.destroy();
	});
});
