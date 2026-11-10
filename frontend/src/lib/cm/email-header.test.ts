import { afterEach, describe, expect, it, vi } from 'vitest';
import { EditorState } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { addedRecipients, badRecipientRanges, emailHeader } from './email-header';

const HEAD = ['From: Alex Rivera <alex@example.com>', 'To: Dana Whitfield <dana@example.com>', 'Subject: Saturday'];
let view: EditorView | null = null;
// jsdom has no layout, so no text boxes: an empty list unless a test lays the row out.
Range.prototype.getClientRects ??= () => [] as unknown as DOMRectList;

function mount(lines: string[], readOnly = false, answered: string[] | null = null, bad: string[] = []) {
	const parent = document.body.appendChild(document.createElement('div'));
	const extensions = [emailHeader({ quiet: [], bad, answered, attachmentsKey: '', mountAttachments: null }), ...(readOnly ? [EditorState.readOnly.of(true)] : [])];
	view = new EditorView({ parent, state: EditorState.create({ doc: `${lines.join('\n')}\n\nHi Dana,\n`, extensions }) });
	return view;
}
const button = () => document.querySelector<HTMLButtonElement>('.cm-email-cc-add');

afterEach(() => { view?.destroy(); view = null; document.body.replaceChildren(); });

describe('the Cc button', () => {
	it('is on the To line while Cc is absent, and adds an empty Cc line with the caret in it', () => {
		const editor = mount(HEAD);
		expect(button()?.textContent).toBe('Cc');
		button()!.click();
		expect(editor.state.doc.line(3).text).toBe('Cc: ');
		expect(editor.state.doc.line(4).text).toBe('Subject: Saturday');
		expect(editor.state.selection.main.head).toBe(editor.state.doc.line(3).to);
		// The Cc line is now the field: the button is gone and the empty line shows a hint.
		expect(button()).toBeNull();
		expect(document.querySelector('.cm-email-address-hint')?.textContent).toBe('name@example.com');
		expect(document.querySelector('.cm-email-head-empty')).not.toBeNull();
	});

	it('is gone while an empty Cc line is there, and the line keeps its hint', () => {
		const editor = mount([HEAD[0], HEAD[1], 'Cc: ', HEAD[2]]);
		expect(button()).toBeNull();
		expect(editor.state.doc.toString().match(/^Cc:/gim)).toHaveLength(1);
		expect(document.querySelector('.cm-email-address-hint')).not.toBeNull();
	});

	it('is gone once Cc has an address, and the line stays', () => {
		const editor = mount([HEAD[0], HEAD[1], 'Cc: priya@example.com', HEAD[2]]);
		expect(button()).toBeNull();
		expect(editor.state.doc.line(3).text).toBe('Cc: priya@example.com');
		expect(document.querySelector('.cm-email-address-hint')).toBeNull();
	});

	it('is gone from a read-only draft, and an empty Cc line there shows no field', () => {
		mount(HEAD, true);
		expect(button()).toBeNull();
		view!.destroy();
		mount([HEAD[0], HEAD[1], 'Cc: ', HEAD[2]], true);
		expect(document.querySelector('.cm-email-address-hint')).toBeNull();
	});
});

describe('an added recipient', () => {
	const ANSWERED = ['dana@example.com'];
	// An entry is its amber head and its no-wrap tail, which sit side by side; read them as one.
	const marks = () => {
		const out: string[] = [];
		document.querySelectorAll('.cm-email-added, .cm-email-added-tail').forEach((node) => {
			const text = node.classList.contains('cm-email-added-tail') ? node.textContent!.replace(/!Not on the email you’re replying to\.$/, '') : node.textContent!;
			if (node.previousElementSibling?.matches('.cm-email-added') && node.classList.contains('cm-email-added-tail')) out[out.length - 1] += text;
			else out.push(text);
		});
		return out;
	};

	it('is the address in To or Cc that was not on the email being answered, amber, with a "!" that says why', () => {
		mount([HEAD[0], 'To: Dana Whitfield <dana@example.com>, Priya Raman <priya@example.com>', 'Cc: marcus@example.com', HEAD[2]], false, ANSWERED);
		expect(marks()).toEqual(['Priya Raman <priya@example.com>', 'marcus@example.com']);
		const bangs = document.querySelectorAll<HTMLButtonElement>('.cm-email-added-mark');
		expect(bangs).toHaveLength(2);
		expect(bangs[0].getAttribute('aria-label')).toBe('Not on the email you’re replying to.');
		expect(bangs[0].querySelector('.cm-email-added-tip')?.textContent).toBe('Not on the email you’re replying to.');
	});

	it('keeps the "!" in one no-wrap piece with the end of its address, so it is never alone on a line', () => {
		mount([HEAD[0], 'To: Priya Raman <priya@example.com>', HEAD[2]], false, ANSWERED);
		const tail = document.querySelector<HTMLElement>('.cm-email-added-tail')!;
		expect(tail.contains(document.querySelector('.cm-email-added-mark'))).toBe(true);
		expect(tail.textContent).toBe('e.com>!Not on the email you’re replying to.');
		expect(tail.previousElementSibling?.textContent).toBe('Priya Raman <priya@exampl');
	});

	it('marks nothing when every address was on the email, or when there is no email to compare with', () => {
		mount(HEAD, false, ['dana@example.com']);
		expect(marks()).toEqual([]);
		view?.destroy(); document.body.replaceChildren();
		mount(HEAD, false, null);
		expect(marks()).toEqual([]);
	});

	it('leaves an address that is still being typed alone, and opens and closes its text on a tap', () => {
		expect(addedRecipients(' dana@example.com, priya@exam', 3, new Set(['dana@example.com']))).toEqual([]);
		mount([HEAD[0], 'To: priya@example.com', HEAD[2]], false, ANSWERED);
		const bang = document.querySelector<HTMLButtonElement>('.cm-email-added-mark')!;
		expect(bang.hasAttribute('data-open')).toBe(false);
		bang.click();
		expect(bang.hasAttribute('data-open')).toBe(true);
		document.body.dispatchEvent(new Event('pointerdown', { bubbles: true }));
		expect(bang.hasAttribute('data-open')).toBe(false);
	});
});

describe('a bad address', () => {
	it('is amber where it sits in To or Cc, with no "!", and the other entries are left alone', () => {
		mount([HEAD[0], 'To: Dana Whitfield <dana@example.com>, priya.raman', 'Cc: dana@', HEAD[2]], false, ['dana@example.com'], ['priya.raman', 'dana@']);
		expect([...document.querySelectorAll('.cm-email-added')].map((node) => node.textContent)).toEqual(['priya.raman', 'dana@']);
		expect(document.querySelector('.cm-email-added-mark')).toBeNull();
		expect(document.querySelector('.cm-email-added-tip')).toBeNull();
	});

	it('marks nothing when the safeguard named nothing, and finds an entry by its text as typed', () => {
		mount(HEAD);
		expect(document.querySelector('.cm-email-added')).toBeNull();
		expect(badRecipientRanges(' dana@example.com, priya.raman', 3, new Set(['priya.raman']))).toEqual([{ from: 22, to: 33 }]);
		expect(badRecipientRanges(' dana@example.com, priya.raman', 3, new Set(['priya']))).toEqual([]);
	});
});

describe('the "!" hint', () => {
	// jsdom has no layout: give the editor, the mark, the hint and the text around the mark the boxes a real page would, on one row.
	const ROW = { top: 0, bottom: 20 };
	const TEXT_WIDTH = 230;
	const rect = (left: number, width: number) => ({ ...ROW, left, right: left + width, width, height: 20, x: left, y: 0, toJSON: () => ({}) }) as DOMRect;

	/** `editor` is the editor's right edge, `markLeft` where the "!" sits; the hint is placed by its data-side and left as the CSS would. */
	function layout(editor: number, markLeft: number, text: { from: string; left: number } | null) {
		const spy = vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
			if (this.classList.contains('cm-scroller')) return rect(0, editor);
			if (this.classList.contains('cm-email-added-mark')) return rect(markLeft, 12);
			if (this.classList.contains('cm-email-cc-add')) return rect(editor - 30, 24);
			if (!this.classList.contains('cm-email-added-tip')) return rect(0, 0);
			const side = this.getAttribute('data-side');
			if (side === 'left') return rect(markLeft - TEXT_WIDTH, TEXT_WIDTH);
			if (side === 'clamp') return rect(markLeft + parseFloat(this.style.left), TEXT_WIDTH);
			return rect(markLeft + 12 + 4, TEXT_WIDTH);
		});
		// Text before the mark starts at 40 (the value column); `text.from` is a piece of text after the mark, at `text.left`.
		const ranges = vi.spyOn(Range.prototype, 'getClientRects').mockImplementation(function (this: Range) {
			const data = this.startContainer.textContent ?? '';
			if (text && data.includes(text.from)) return [rect(text.left, 60)] as unknown as DOMRectList;
			return [rect(40, markLeft - 40)] as unknown as DOMRectList;
		});
		return () => { spy.mockRestore(); ranges.mockRestore(); };
	}
	const tip = () => document.querySelector<HTMLElement>('.cm-email-added-tip')!;
	const open = () => document.querySelector<HTMLButtonElement>('.cm-email-added-mark')!.click();

	it('opens just right of the "!" where it fits, on the mark\'s own row', () => {
		const restore = layout(1000, 200, null);
		mount([HEAD[0], 'To: priya@example.com', HEAD[2]], false, ['dana@example.com']);
		open();
		expect(tip().getAttribute('data-side')).toBe('right');
		expect(tip().style.maxWidth).toBe('');
		expect(tip().getBoundingClientRect().left).toBeGreaterThanOrEqual(212);
		restore();
	});

	it('ends at the "!" and opens leftward when it does not fit on a phone, never past the start of its own address', () => {
		const restore = layout(360, 300, null);
		mount([HEAD[0], 'To: priya@example.com', HEAD[2]], false, ['dana@example.com']);
		open();
		expect(tip().getAttribute('data-side')).toBe('left');
		const box = tip().getBoundingClientRect();
		expect(box.right).toBe(300);
		expect(box.left).toBeGreaterThanOrEqual(40);
		restore();
	});

	it('stays one line on the "!" row, as close to the "!" as fits and inside the editor, when neither side has room for it', () => {
		const restore = layout(360, 150, null);
		mount([HEAD[0], 'To: a@b.co', HEAD[2]], false, ['dana@example.com']);
		open();
		expect(tip().getAttribute('data-side')).toBe('clamp');
		const box = tip().getBoundingClientRect();
		expect(box.width).toBe(TEXT_WIDTH);
		expect(box.left).toBe(122);
		expect(box.right).toBe(352);
		expect(tip().style.whiteSpace).toBe('');
		restore();
	});

	it('does the same with a recipient after the short address on the row, and starts at the left edge of the editor when it is narrower than the hint', () => {
		const restore = layout(230, 150, { from: 'dana@example.com', left: 170 });
		mount([HEAD[0], 'To: a@b.co, dana@example.com', HEAD[2]], false, ['dana@example.com']);
		open();
		expect(tip().getAttribute('data-side')).toBe('clamp');
		expect(tip().getBoundingClientRect().left).toBe(8);
		restore();
	});

	it('opens leftward, not over the next address on the row, even where there is room on the right', () => {
		const restore = layout(1000, 300, { from: 'dana@example.com', left: 316 });
		mount([HEAD[0], 'To: priya@example.com, dana@example.com', HEAD[2]], false, ['dana@example.com']);
		open();
		expect(tip().getAttribute('data-side')).toBe('left');
		expect(tip().getBoundingClientRect().right).toBe(300);
		restore();
	});

	it('is placed again each time it opens, with no above or below flip left over', () => {
		const restore = layout(1000, 200, null);
		mount([HEAD[0], 'To: priya@example.com', HEAD[2]], false, ['dana@example.com']);
		open();
		open();
		open();
		expect(tip().hasAttribute('data-below')).toBe(false);
		expect(tip().getAttribute('data-side')).toBe('right');
		restore();
	});
});
