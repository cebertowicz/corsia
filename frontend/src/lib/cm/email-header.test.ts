import { afterEach, describe, expect, it, vi } from 'vitest';
import { EditorSelection, EditorState, Transaction } from '@codemirror/state';
import { EditorView } from '@codemirror/view';
import { history, undo } from '@codemirror/commands';
import { addedRecipients, badRecipientRanges, emailHeader } from './email-header';

const HEAD = ['From: alex@example.com', 'To: dana@example.com', 'Subject: Saturday'];
let view: EditorView | null = null;

function mount(lines: string[], readOnly = false, answered: string[] | null = null) {
	const parent = document.body.appendChild(document.createElement('div'));
	const extensions = [history(), emailHeader({ quiet: [], answered, attachmentsKey: '', mountAttachments: null }), ...(readOnly ? [EditorState.readOnly.of(true)] : [])];
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
		mount([HEAD[0], 'To: dana@example.com, priya@example.com', 'Cc: marcus@example.com', HEAD[2]], false, ANSWERED);
		expect(marks()).toEqual(['priya@example.com', 'marcus@example.com']);
		const bangs = document.querySelectorAll<HTMLButtonElement>('.cm-email-added-mark');
		expect(bangs).toHaveLength(2);
		expect(bangs[0].getAttribute('aria-label')).toBe('Not on the email you’re replying to.');
		expect(bangs[0].querySelector('.cm-email-added-tip')?.textContent).toBe('Not on the email you’re replying to.');
	});

	it('compares addresses without case', () => {
		mount([HEAD[0], 'To: Dana@Example.com', HEAD[2]], false, ANSWERED);
		expect(marks()).toEqual([]);
	});

	it('keeps the "!" in one no-wrap piece with the end of its address, so it is never alone on a line', () => {
		mount([HEAD[0], 'To: priya@example.com', HEAD[2]], false, ANSWERED);
		const tail = document.querySelector<HTMLElement>('.cm-email-added-tail')!;
		expect(tail.contains(document.querySelector('.cm-email-added-mark'))).toBe(true);
		expect(tail.textContent).toBe('le.com!Not on the email you’re replying to.');
		expect(tail.previousElementSibling?.textContent).toBe('priya@examp');
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
	it('is every entry in To or Cc the send would refuse, amber where it sits, with no "!", and the other entries are left alone', () => {
		mount([HEAD[0], 'To: dana@example.com, priya.raman', 'Cc: dana@, Sam Lee <sam@example.com>', HEAD[2]], false, ['dana@example.com']);
		expect([...document.querySelectorAll('.cm-email-bad')].map((node) => node.textContent)).toEqual(['priya.raman', 'dana@', 'Sam Lee <sam@example.com>']);
		expect(document.querySelector('.cm-email-added')).toBeNull();
		expect(document.querySelector('.cm-email-added-mark')).toBeNull();
	});

	it('has its own class, apart from an added recipient', () => {
		mount([HEAD[0], 'To: priya@example.com, priya.raman', HEAD[2]], false, ['dana@example.com']);
		expect(document.querySelectorAll('.cm-email-bad')).toHaveLength(1);
		expect(document.querySelector('.cm-email-bad')!.classList.contains('cm-email-added')).toBe(false);
		expect(document.querySelectorAll('.cm-email-added-mark')).toHaveLength(1);
	});

	it('marks nothing when every entry is a full address, and finds an entry by its text as typed', () => {
		mount(HEAD);
		expect(document.querySelector('.cm-email-bad')).toBeNull();
		expect(badRecipientRanges(' dana@example.com, priya.raman', 3)).toEqual([{ from: 22, to: 33 }]);
	});

	it('reads an old "In reply to:" line as a plain line, not a field', () => {
		mount([HEAD[0], HEAD[1], 'In reply to: Dana, Nov 3', HEAD[2]]);
		expect(document.querySelector('.cm-email-bad')).toBeNull();
		// Labelled: From, To and Subject only.
		expect([...document.querySelectorAll('.cm-email-head-label')].map((node) => node.textContent)).toEqual(['From:', 'To:', 'Subject:']);
	});
});

describe('the From line', () => {
	const from = (editor: EditorView) => editor.state.doc.line(1).text;
	const type = (editor: EditorView, text: string) =>
		editor.dispatch(editor.state.update({ changes: { from: 0, to: editor.state.doc.length, insert: text }, userEvent: 'input.type' }));

	it('keeps From when the person selects everything and types: what they typed is the rest of the draft', () => {
		const editor = mount(HEAD);
		editor.dispatch({ selection: EditorSelection.single(0, editor.state.doc.length) });
		type(editor, 'x');
		expect(editor.state.doc.toString()).toBe('From: alex@example.com\nx');
		expect(editor.state.selection.main.head).toBe(editor.state.doc.length);
	});

	it('keeps From when the person selects everything and deletes', () => {
		const editor = mount(HEAD);
		editor.dispatch(editor.state.update({ changes: { from: 0, to: editor.state.doc.length }, userEvent: 'delete.selection' }));
		expect(editor.state.doc.toString()).toBe('From: alex@example.com\n');
	});

	it('applies the rest of an edit that crosses From, and an undo puts the whole draft back', () => {
		const editor = mount(HEAD);
		const before = editor.state.doc.toString();
		// From the middle of From to the end of To, replaced with typed text.
		editor.dispatch(editor.state.update({ changes: { from: 8, to: 43, insert: 'To: sam@example.com' }, userEvent: 'input.type' }));
		expect(from(editor)).toBe('From: alex@example.com');
		expect(editor.state.doc.line(2).text).toBe('To: sam@example.com');
		expect(editor.state.doc.line(3).text).toBe('Subject: Saturday');
		undo(editor);
		expect(editor.state.doc.toString()).toBe(before);
	});

	it('leaves an edit that does not reach From as it is, and lets an agent or remote write change it', () => {
		const editor = mount(HEAD);
		editor.dispatch(editor.state.update({ changes: { from: 27, insert: 'x' }, userEvent: 'input.type' }));
		expect(editor.state.doc.line(2).text).toBe('To: xdana@example.com');
		editor.dispatch(editor.state.update({ changes: { from: 6, to: 22, insert: 'sam@example.com' }, annotations: Transaction.remote.of(true) }));
		expect(from(editor)).toBe('From: sam@example.com');
	});
});

describe('the "!" hint', () => {
	// jsdom has no layout: give the editor, the mark and the hint the boxes a real page would.
	const TEXT_WIDTH = 230;
	const rect = (left: number, width: number, top = 0) => ({ top, bottom: top + 20, left, right: left + width, width, height: 20, x: left, y: top, toJSON: () => ({}) }) as DOMRect;

	/** `editor` is the editor's left edge, `markLeft` where the "!" sits; the hint ends at the mark unless it was clamped, as the CSS places it. */
	function layout(editor: number, markLeft: number) {
		return vi.spyOn(HTMLElement.prototype, 'getBoundingClientRect').mockImplementation(function (this: HTMLElement) {
			if (this.classList.contains('cm-scroller')) return rect(editor, 1000);
			if (this.classList.contains('cm-email-added-mark')) return rect(markLeft, 12);
			if (!this.classList.contains('cm-email-added-tip')) return rect(0, 0);
			const left = this.hasAttribute('data-clamped') ? markLeft + parseFloat(this.style.left) : markLeft + 12 - TEXT_WIDTH;
			return rect(left, TEXT_WIDTH, 24);
		});
	}
	const tip = () => document.querySelector<HTMLElement>('.cm-email-added-tip')!;
	const open = () => document.querySelector<HTMLButtonElement>('.cm-email-added-mark')!.click();

	it('opens below the "!" row and ends at the "!", so it never covers its own address', () => {
		const spy = layout(0, 300);
		mount([HEAD[0], 'To: priya@example.com', HEAD[2]], false, ['dana@example.com']);
		open();
		expect(tip().hasAttribute('data-clamped')).toBe(false);
		expect(tip().style.left).toBe('');
		spy.mockRestore();
	});

	it('starts at the editor\'s left edge when ending at the "!" would cut it', () => {
		const spy = layout(0, 100);
		mount([HEAD[0], 'To: priya@example.com', HEAD[2]], false, ['dana@example.com']);
		open();
		expect(tip().hasAttribute('data-clamped')).toBe(true);
		expect(tip().getBoundingClientRect().left).toBe(8);
		spy.mockRestore();
	});

	it('is placed again each time it opens', () => {
		const spy = layout(0, 100);
		mount([HEAD[0], 'To: priya@example.com', HEAD[2]], false, ['dana@example.com']);
		open();
		open();
		spy.mockImplementation(function (this: HTMLElement) {
			if (this.classList.contains('cm-email-added-tip')) return rect(this.hasAttribute('data-clamped') ? 0 : 300 - TEXT_WIDTH + 12, TEXT_WIDTH, 24);
			return rect(this.classList.contains('cm-email-added-mark') ? 300 : 0, 12);
		});
		open();
		expect(tip().hasAttribute('data-clamped')).toBe(false);
		spy.mockRestore();
	});
});
