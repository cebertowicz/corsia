import { writable } from 'svelte/store';

export type CanvasHeaderAction = {
	id: string;
	label: string;
	onClick: () => void;
	active?: boolean;
	primary?: boolean;
	title?: string;
	wideOnly?: boolean;
	/** Shown but not pressable (Activity's Next on the last item). */
	disabled?: boolean;
};

/** The title as a control: a document's name opens its open-files list. */
export type CanvasHeaderTitleAction = {
	/** What the name opens, read after it ("Open files, 3"). */
	label: string;
	title: string;
	onClick: () => void;
	/** Another open file changed since it was opened. */
	changed?: boolean;
};

export type CanvasHeaderState = {
	title: string;
	titleAction?: CanvasHeaderTitleAction;
	onBack?: () => void;
	backLabel?: string;
	/** Back and the enabled actions read as links, and Back keeps its label on a phone (an Activity item). */
	links?: boolean;
	tag?: string;
	actions?: CanvasHeaderAction[];
} | null;

export const canvasHeader = writable<CanvasHeaderState>(null);
