import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import { eventOrigin, keepTyped, keptValue } from './keepTyped';

// jsdom can't make a trusted event, so the events a keyboard would send are marked here and the seam trusts only those.
const fromDevice = new WeakSet<Event>();
const device = <T extends Event>(event: T) => { fromDevice.add(event); return event; };

let field: HTMLTextAreaElement;
let seen: string[];
let action: { destroy(): void };

beforeEach(() => {
	vi.spyOn(eventOrigin, 'trusted').mockImplementation((event) => fromDevice.has(event));
	field = document.createElement('textarea');
	document.body.append(field);
	seen = [];
	// Registered before the action, like `bind:value`: it must only ever read the kept text.
	field.addEventListener('input', () => seen.push(field.value));
	action = keepTyped(field);
});
afterEach(() => {
	action.destroy();
	vi.restoreAllMocks();
	document.body.replaceChildren();
});

function typed(value: string, caret = value.length, end = caret) {
	field.value = value;
	field.setSelectionRange(caret, end);
}

/** One keyboard edit: `beforeinput`, the browser's change to the value, then `input`. */
function edit(inputType: string, next: string, options: { composing?: boolean; plain?: boolean; before?: boolean } = {}) {
	const init = { inputType, bubbles: true, cancelable: true, isComposing: !!options.composing };
	if (options.before !== false) field.dispatchEvent(device(new InputEvent('beforeinput', init)));
	field.value = next;
	field.dispatchEvent(device(options.plain ? new Event('input', { bubbles: true }) : new InputEvent('input', init)));
}

describe('an AutoFill suggestion that replaces the whole value', () => {
	it.each(['insertReplacementText', 'insertText'])('keeps the typed text and inserts the suggestion at the caret (%s)', (type) => {
		typed('Thanks, see you at');
		edit(type, 'Sam Rivera');
		expect(field.value).toBe('Thanks, see you at Sam Rivera');
		expect(field.selectionStart).toBe(field.value.length);
	});

	it('keeps the typed text when the input event has no inputType', () => {
		typed('Call me on');
		edit('', '555 0100', { plain: true });
		expect(field.value).toBe('Call me on 555 0100');
	});

	it('inserts at a caret in the middle', () => {
		typed('Call me  tomorrow', 8);
		edit('insertReplacementText', '555 0100');
		expect(field.value).toBe('Call me 555 0100 tomorrow');
		expect(field.selectionStart).toBe('Call me 555 0100'.length);
	});

	it('brings back the whole suggestion when it shares a few letters with the typed text', () => {
		typed('Hello to you');
		edit('insertReplacementText', 'Hey Sam');
		expect(field.value).toBe('Hello to you Hey Sam');
	});

	it("cuts the suggestion to fit the field's maxlength", () => {
		field.maxLength = 25;
		typed('Thanks, see you at');
		edit('insertReplacementText', 'Sam Rivera');
		expect(field.value).toBe('Thanks, see you at Sam Ri');
	});

	it('keeps the typed text alone when a full field has no room for the suggestion', () => {
		field.maxLength = 18;
		typed('Thanks, see you at');
		edit('insertReplacementText', 'Sam Rivera');
		expect(field.value).toBe('Thanks, see you at');
		expect(field.selectionStart).toBe(18);
	});

	it('repairs before handlers registered earlier read the value', () => {
		typed('Looks good to');
		edit('insertReplacementText', 'Sam');
		expect(seen).toEqual(['Looks good to Sam']);
	});
});

describe('edits that stand', () => {
	it('autocorrect replacing the word at the caret', () => {
		typed('fix teh');
		edit('insertReplacementText', 'fix the');
		expect(field.value).toBe('fix the');
	});

	it('autocorrect replacing the word before the caret after a space', () => {
		typed('fix teh ');
		edit('insertReplacementText', 'fix the ');
		expect(field.value).toBe('fix the ');
	});

	it('a contextual fix of the word before the one the caret is in', () => {
		typed('I think their is');
		edit('insertReplacementText', 'I think there is');
		expect(field.value).toBe('I think there is');
	});

	it('a contextual fix of the word before the last one, after a space', () => {
		typed('I think their is ');
		edit('insertReplacementText', 'I think there is ');
		expect(field.value).toBe('I think there is ');
	});

	it('a spelling-menu fix of a word away from the caret', () => {
		typed('I recieved the file and will reply soon');
		edit('insertReplacementText', 'I received the file and will reply soon');
		expect(field.value).toBe('I received the file and will reply soon');
	});

	it('a QuickType completion of the word at the caret', () => {
		typed('Hi Sa');
		edit('insertReplacementText', 'Hi Sam ');
		expect(field.value).toBe('Hi Sam ');
	});

	it('a text replacement shortcut', () => {
		typed("I'm omw");
		edit('insertReplacementText', "I'm On my way!");
		expect(field.value).toBe("I'm On my way!");
	});

	it('select-all, then type', () => {
		typed('old draft', 0, 9);
		edit('insertText', 'n');
		expect(field.value).toBe('n');
	});

	it('select-all, then paste', () => {
		typed('old draft', 0, 9);
		edit('insertFromPaste', 'pasted text');
		expect(field.value).toBe('pasted text');
	});

	it('a partial selection, then type', () => {
		typed('Hello big world today', 6, 15);
		edit('insertText', 'Hello x today');
		expect(field.value).toBe('Hello x today');
	});

	it.each(['deleteByCut', 'deleteContentBackward', 'deleteWordBackward', 'deleteSoftLineBackward'])('%s', (type) => {
		typed('first line and more');
		edit(type, '');
		expect(field.value).toBe('');
	});

	it.each(['historyUndo', 'historyRedo'])('%s', (type) => {
		typed('after');
		edit(type, 'something else entirely');
		expect(field.value).toBe('something else entirely');
	});

	it('IME composition', () => {
		typed('今日は ');
		field.dispatchEvent(new CompositionEvent('compositionstart'));
		edit('insertCompositionText', 'かな');
		edit('insertText', 'カナ', { composing: true });
		field.dispatchEvent(new CompositionEvent('compositionend'));
		expect(field.value).toBe('カナ');
	});

	it('dictation inserting at the caret', () => {
		typed('Meeting at');
		edit('insertText', 'Meeting at three');
		expect(field.value).toBe('Meeting at three');
	});

	it('a long multi-line replacement, such as a dictated paragraph or a whole-field rewrite', () => {
		typed('rough notes about the plan');
		const rewrite = 'Here is a clearer version of the plan.\nFirst, we ship the fix.\nThen we check it on a phone.';
		edit('insertReplacementText', rewrite);
		expect(field.value).toBe(rewrite);
	});

	it('a one-line replacement longer than a contact suggestion', () => {
		typed('short note here');
		const long = 'x'.repeat(121);
		edit('insertReplacementText', long);
		expect(field.value).toBe(long);
	});

	it('a field cleared on send never brings the sent text back', () => {
		typed('sent comment');
		field.value = '';
		edit('insertReplacementText', 'Sam');
		expect(field.value).toBe('Sam');
		field.value = '';
		edit('insertText', 'a');
		expect(field.value).toBe('a');
	});

	it("our own synthetic input, such as the mention picker's", () => {
		typed('Hello @');
		field.value = 'Hello @Alpha Agent ';
		field.dispatchEvent(new Event('input', { bubbles: true }));
		expect(field.value).toBe('Hello @Alpha Agent ');
	});

	it('an edit with no beforeinput', () => {
		typed('Thanks, see you at');
		edit('insertReplacementText', 'Sam', { before: false });
		expect(field.value).toBe('Sam');
	});

	it.each(['Thanks', 'Thanks Sam'])('a field of one or two words (%s), which looks the same as a word fix', (value) => {
		typed(value);
		edit('insertReplacementText', 'Sam');
		expect(field.value).toBe('Sam');
	});

	it('after the action is destroyed', () => {
		action.destroy();
		typed('Thanks, see you at');
		edit('insertReplacementText', 'Sam');
		expect(field.value).toBe('Sam');
		action = keepTyped(field);
	});
});

describe('typing hints', () => {
	it("gives a prose field the chat input's hints", () => {
		expect(['autocomplete', 'autocorrect', 'autocapitalize', 'spellcheck'].map((name) => field.getAttribute(name))).toEqual(['off', 'on', 'sentences', 'true']);
	});

	it('keeps a hint the field sets itself', () => {
		const own = document.createElement('textarea');
		own.setAttribute('spellcheck', 'false');
		keepTyped(own).destroy();
		expect(own.getAttribute('spellcheck')).toBe('false');
	});

	it('gives a name field the repair alone', () => {
		const name = document.createElement('input');
		keepTyped(name, { prose: false }).destroy();
		expect(['autocomplete', 'autocorrect', 'autocapitalize', 'spellcheck'].some((hint) => name.hasAttribute(hint))).toBe(false);
	});
});

describe('keptValue', () => {
	it('leaves an empty or whitespace-only field alone', () => {
		expect(keptValue({ value: '  ', start: 2, end: 2 }, 'Sam')).toBeNull();
	});

	it('leaves a pure insertion alone', () => {
		expect(keptValue({ value: 'Hi ', start: 3, end: 3 }, 'Hi Sam')).toBeNull();
	});
});
