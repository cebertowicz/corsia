import { writable } from 'svelte/store';
import type { DocSaveActivity } from './docSaveQueue';

export const idleDocSaveActivity: DocSaveActivity = {
	writeInFlight: false,
	resaveQueued: false,
	awaitingReconnect: false,
	confirmationOwed: false,
};

export const docSaveActivities = writable<Record<string, DocSaveActivity>>({});

export function publishDocSaveActivity(key: string, activity: DocSaveActivity): void {
	docSaveActivities.update((all) => ({ ...all, [key]: activity }));
}

export function clearDocSaveActivity(key: string): void {
	docSaveActivities.update((all) => {
		if (!(key in all)) return all;
		const next = { ...all };
		delete next[key];
		return next;
	});
}
