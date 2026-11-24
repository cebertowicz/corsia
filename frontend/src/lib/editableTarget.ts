/** Whether a focus target takes typed text, so a soft keyboard opens for it. */
export function isEditableTarget(target: EventTarget | null): boolean {
	const element = target as HTMLElement | null;
	if (!element) return false;
	if (element.tagName === 'TEXTAREA') return true;
	if (element.tagName === 'INPUT') {
		const type = (element as HTMLInputElement).type;
		return type !== 'file' && type !== 'submit' && type !== 'button';
	}
	return element.isContentEditable === true;
}
