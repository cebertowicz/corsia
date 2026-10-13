import { writable } from 'svelte/store';

/** Read-only queue state used by the cosmetic save cue. */
export interface DocSaveActivity {
	writeInFlight: boolean;
	resaveQueued: boolean;
	awaitingReconnect: boolean;
	confirmationOwed: boolean;
}

export const idleDocSaveActivity: DocSaveActivity = {
	writeInFlight: false,
	resaveQueued: false,
	awaitingReconnect: false,
	confirmationOwed: false,
};

/** The queue retains the maps and the only writer; consumers receive subscribe only. */
export function createDocSaveActivityProjection(maps: {
	inflight: ReadonlyMap<string, Promise<boolean>>;
	needsResave: ReadonlyMap<string, boolean>;
	reconnectWaits: ReadonlyMap<string, () => void>;
	confirmationsOwed: ReadonlyMap<string, true>;
}) {
	const activity = writable<Record<string, DocSaveActivity>>({});
	return {
		store: { subscribe: activity.subscribe },
		publish(key: string): void {
			activity.update((all) => ({
				...all,
				[key]: {
					writeInFlight: maps.inflight.has(key),
					resaveQueued: maps.needsResave.has(key),
					awaitingReconnect: maps.reconnectWaits.has(key),
					confirmationOwed: maps.confirmationsOwed.has(key),
				},
			}));
		},
		clear(key: string): void {
			activity.update((all) => {
				if (!(key in all)) return all;
				const next = { ...all };
				delete next[key];
				return next;
			});
		},
	};
}
