import { Compartment, StateEffect } from '@codemirror/state';
import type { EditorView } from '@codemirror/view';
import { emailHeader, type EmailHeaderOptions } from '$lib/cm/email-header';

export type { EmailHeaderOptions };

/**
 * An editor's email header slot. The header's extension joins the editor the first time `options()` is not null and follows
 * it from then on (null again leaves a plain document); a document that is no email draft never gets the slot.
 * Call it while the component is being set up, like `$effect`.
 */
export function useEmailHeader(view: () => EditorView | undefined, options: () => EmailHeaderOptions | null): void {
	const slot = new Compartment();
	let addedTo: EditorView | undefined;
	$effect(() => {
		const v = view(), next = options();
		if (!v || (!next && addedTo !== v)) return;
		const extension = next ? emailHeader(next) : [];
		v.dispatch({ effects: addedTo === v ? slot.reconfigure(extension) : StateEffect.appendConfig.of(slot.of(extension)) });
		addedTo = v;
	});
}
