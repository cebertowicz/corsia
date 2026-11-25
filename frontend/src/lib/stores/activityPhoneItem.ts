import { writable } from 'svelte/store';

/**
 * On a phone, the Activity item open on screen: the app's own header carries the way back to the list. Activity sets
 * it while an item is open on a phone and clears it otherwise; the header reads it through `syncCanvasHeader`.
 */
export interface ActivityPhoneItem {
	backLabel: 'Your tasks' | 'Notifications';
	onBack: () => void;
}

export const activityPhoneItem = writable<ActivityPhoneItem | null>(null);
