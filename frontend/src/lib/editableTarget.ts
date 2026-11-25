/** Input types that take no typed text, so no soft keyboard opens for them. */
const NO_KEYBOARD_INPUT_TYPES = new Set(['file', 'submit', 'button', 'reset', 'image', 'hidden', 'checkbox', 'radio', 'range', 'color']);

/** Whether a focus target takes typed text, so a soft keyboard opens for it. */
export function isEditableTarget(target: EventTarget | null): boolean {
	const element = target as HTMLElement | null;
	if (!element) return false;
	if (element.tagName === 'TEXTAREA') return true;
	if (element.tagName === 'INPUT') return !NO_KEYBOARD_INPUT_TYPES.has((element as HTMLInputElement).type);
	return element.isContentEditable === true;
}
