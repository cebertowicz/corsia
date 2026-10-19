import { tick } from 'svelte';

export async function setContentEditableText(
	input: HTMLElement | undefined,
	text: string,
	autoResize: (input: HTMLElement) => void,
	placeCaretAtEnd: (input: HTMLElement) => void,
): Promise<void> {
	await tick();
	if (!input) return;
	input.textContent = text;
	autoResize(input);
	placeCaretAtEnd(input);
}
