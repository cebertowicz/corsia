/**
 * Grow a text entry surface to its content until the shared composer limit,
 * then keep the overflow available inside the control.
 */
export function resizeTextInputToContent(element: HTMLElement, maxHeight: number): number {
	element.style.height = 'auto';
	const contentHeight = element.scrollHeight;
	const height = Math.min(contentHeight, maxHeight);
	element.style.height = `${height}px`;
	element.style.overflowY = contentHeight > maxHeight ? 'auto' : 'hidden';
	return height;
}
