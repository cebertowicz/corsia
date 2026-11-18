import { tick } from 'svelte';
import { returnFocus } from '$lib/focusReturn';

/** Return to chat before restoring the composer's focus. */
export function focusComposerAfterUpdate(returnToChat?: () => void | Promise<void>): void {
	void Promise.resolve(returnToChat?.()).then(tick).then(() => {
		returnFocus(document.querySelector<HTMLElement>('[role="textbox"][aria-label="Message CORSIA"]'));
	});
}
