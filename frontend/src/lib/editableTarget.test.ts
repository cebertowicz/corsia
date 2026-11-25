import { expect, it } from 'vitest';
import { isEditableTarget } from './editableTarget';

const input = (type: string) => { const el = document.createElement('input'); el.type = type; return el; };

it('counts the fields a soft keyboard opens for', () => {
	expect(isEditableTarget(input('text'))).toBe(true);
	expect(isEditableTarget(input('email'))).toBe(true);
	expect(isEditableTarget(input('date'))).toBe(true);
	expect(isEditableTarget(document.createElement('textarea'))).toBe(true);
	const editable = document.createElement('div');
	Object.defineProperty(editable, 'isContentEditable', { value: true });
	expect(isEditableTarget(editable)).toBe(true);
});

it('counts no control that takes no typed text', () => {
	for (const type of ['checkbox', 'radio', 'range', 'color', 'file', 'submit', 'button', 'reset', 'image', 'hidden']) expect(isEditableTarget(input(type)), type).toBe(false);
	expect(isEditableTarget(document.createElement('button'))).toBe(false);
	expect(isEditableTarget(null)).toBe(false);
});
