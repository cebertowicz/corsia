import { writable } from 'svelte/store';

/**
 * On a phone, the Activity item open on screen: the app's own header carries back to the list and Next. Activity
 * sets it while an item is open on a phone and clears it otherwise; the header reads it through `syncCanvasHeader`.
 */
export interface ActivityPhoneItem {
	position: number;
	count: number;
	nextDisabled: boolean;
	onBack: () => void;
	onNext: () => void;
}

export const activityPhoneItem = writable<ActivityPhoneItem | null>(null);
