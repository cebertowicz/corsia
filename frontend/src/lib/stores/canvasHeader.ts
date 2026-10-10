import { writable } from 'svelte/store';

export type CanvasHeaderAction = {
	id: string;
	label: string;
	onClick: () => void;
	active?: boolean;
	primary?: boolean;
	title?: string;
	wideOnly?: boolean;
};

export type CanvasHeaderState = {
	title: string;
	onBack?: () => void;
	backLabel?: string;
	tag?: string;
	actions?: CanvasHeaderAction[];
} | null;

export const canvasHeader = writable<CanvasHeaderState>(null);
