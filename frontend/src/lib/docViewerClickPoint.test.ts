import { afterEach, describe, expect, it, vi } from 'vitest';
import { clickAnchor, createCommentOpeners } from './docViewerClickPoint';

const ownRects = Range.prototype.getClientRects;
afterEach(() => { document.body.replaceChildren(); vi.restoreAllMocks(); Range.prototype.getClientRects = ownRects; });

const press = (target: Element, init: MouseEventInit) => {
	const event = new MouseEvent('click', { bubbles: true, ...init });
	target.dispatchEvent(event);
	return event;
};

describe('clickAnchor', () => {
	it('has no point without a click, or for a button pressed from the keyboard', () => {
		const button = document.body.appendChild(document.createElement('button'));
		expect(clickAnchor(undefined, () => null)).toBeNull();
		expect(clickAnchor(press(button, { detail: 0, clientX: 5, clientY: 5 }), () => null)).toBeNull();
	});
	it('sits at the pointer, against a margin marker’s own box', () => {
		const marker = document.body.appendChild(document.createElement('button'));
		marker.className = 'cm-comment-marker';
		vi.spyOn(marker, 'getBoundingClientRect').mockReturnValue(new DOMRect(4, 100, 20, 18));
		const rect = clickAnchor(press(marker, { detail: 1, clientX: 12, clientY: 108 }), () => null)!()!;
		expect([rect.x, rect.top, rect.width, rect.height]).toEqual([12, 100, 0, 18]);
	});
	it('sits at the pointer, against the clicked line’s box', () => {
		const line = document.body.appendChild(document.createElement('div'));
		line.textContent = 'alpha beta gamma';
		line.style.lineHeight = '24px';
		// jsdom draws no text: give the paragraph two lines of boxes.
		Range.prototype.getClientRects = () => [new DOMRect(0, 200, 300, 20), new DOMRect(0, 224, 300, 20)] as unknown as DOMRectList;
		const rect = clickAnchor(press(line, { detail: 1, clientX: 90, clientY: 230 }), () => null)!()!;
		expect([rect.x, rect.top, rect.height]).toEqual([90, 222, 24]);
	});
	it('follows its comment’s text as it moves, and hides when the text’s box does', () => {
		const marker = document.body.appendChild(document.createElement('button'));
		marker.className = 'cm-comment-marker';
		vi.spyOn(marker, 'getBoundingClientRect').mockReturnValue(new DOMRect(4, 100, 20, 18));
		let text: DOMRect | null = new DOMRect(40, 100, 200, 18);
		const anchor = clickAnchor(press(marker, { detail: 1, clientX: 12, clientY: 108 }), () => text)!;
		text = new DOMRect(40, 60, 200, 18);
		const moved = anchor()!;
		expect([moved.x, moved.top, moved.height]).toEqual([12, 60, 18]);
		text = null;
		expect(anchor()).toBeNull();
	});
});

describe('createCommentOpeners', () => {
	const openers = () => {
		const ui = { focusComment: vi.fn(() => false), focusMarker: vi.fn() };
		const scroll = { stay: vi.fn(), toComment: vi.fn() };
		return { ui, open: createCommentOpeners(ui as never, scroll as never) };
	};
	it('reads the thread for a click or tap on its highlight, and not for a caret moved in by keys', () => {
		const { ui, open } = openers();
		open.comment('c', () => null);
		expect(ui.focusComment).toHaveBeenLastCalledWith('c', expect.anything(), undefined, false, false);
		Range.prototype.getClientRects = () => [] as unknown as DOMRectList;
		const span = document.body.appendChild(document.createElement('span'));
		// A phone tap has no point to open at, and still reads.
		open.comment('c', () => null, undefined, press(span, { detail: 1 }));
		expect(ui.focusComment).toHaveBeenLastCalledWith('c', expect.anything(), undefined, expect.any(Boolean), true);
	});
});
