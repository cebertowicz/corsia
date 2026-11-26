import { tick } from 'svelte';

interface ResolvedEntryDeps {
	/** The comments drawer's list view is the one showing. */
	listOpen: () => boolean;
	openList: (opener: HTMLElement) => void;
	/** Ends the document-history view, as opening the drawer from the row does. */
	closeHistory: () => void;
	resolvedOpen: () => boolean;
	setResolvedOpen: (open: boolean) => void;
}

/** The row's Resolved control: opens the drawer on its Resolved view (set after the open, which resets it), switches an open one to it, or closes it when it is already there. */
export async function openResolvedView(deps: ResolvedEntryDeps, opener: HTMLElement): Promise<void> {
	if (!deps.listOpen()) {
		deps.closeHistory();
		deps.openList(opener);
		await tick();
		deps.setResolvedOpen(true);
	} else if (deps.resolvedOpen()) deps.openList(opener);
	else deps.setResolvedOpen(true);
}
