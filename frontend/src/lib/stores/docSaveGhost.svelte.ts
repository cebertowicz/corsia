import { confirmReconnectSave, type DocSaveActivity, type DocSaveDraft } from './docSaveQueue';

export type DocSaveIndicator = 'idle' | 'saving' | 'saved';

export interface DocSaveCueSnapshot {
	key: string | null;
	draft: DocSaveDraft | undefined;
	activity?: DocSaveActivity;
}

interface Episode {
	startedAt: number;
	suspendedAt: number | null;
	showingSaving: boolean;
	savingTimer: ReturnType<typeof setTimeout> | null;
	savedTimer: ReturnType<typeof setTimeout> | null;
	showingSaved: boolean;
	settled: boolean;
}

const EPISODE_NOTICE_MS = 400;
const SAVED_NOTICE_MS = 1400;
const inactiveActivity: DocSaveActivity = {
	writeInFlight: false,
	resaveQueued: false,
	awaitingReconnect: false,
	confirmationOwed: false,
};

/**
 * Turns queue facts into one cosmetic, per-draft writing episode. Queue state
 * remains authoritative: this module never starts a save or declares it durable.
 */
export function docSaveGhost(snapshot: () => DocSaveCueSnapshot) {
	let current = $state<DocSaveIndicator>('idle');
	let visibleKey: string | null = null;
	const episodes = new Map<string, Episode>();

	const stopSavingTimer = (episode: Episode) => {
		if (episode.savingTimer) clearTimeout(episode.savingTimer);
		episode.savingTimer = null;
	};
	const stopSavedTimer = (episode: Episode) => {
		if (episode.savedTimer) clearTimeout(episode.savedTimer);
		episode.savedTimer = null;
	};
	const updateVisible = (key: string, episode: Episode) => {
		if (visibleKey === key) current = episode.showingSaved ? 'saved' : episode.showingSaving ? 'saving' : 'idle';
	};
	const scheduleSaving = (key: string, episode: Episode) => {
		stopSavingTimer(episode);
		const remaining = Math.max(0, EPISODE_NOTICE_MS - (Date.now() - episode.startedAt));
		episode.savingTimer = setTimeout(() => {
			episode.savingTimer = null;
			episode.showingSaving = true;
			updateVisible(key, episode);
		}, remaining);
	};
	const showSaved = (key: string, episode: Episode) => {
		stopSavedTimer(episode);
		episode.showingSaved = true;
		updateVisible(key, episode);
		episode.savedTimer = setTimeout(() => {
			episode.savedTimer = null;
			episode.showingSaved = false;
			if (episode.settled) episodes.delete(key);
			updateVisible(key, episode);
		}, SAVED_NOTICE_MS);
	};
	const finishEpisode = (key: string, episode: Episode, confirmationOwed: boolean) => {
		stopSavingTimer(episode);
		const shouldConfirm = episode.showingSaving || confirmationOwed;
		episode.showingSaving = false;
		if (shouldConfirm) showSaved(key, episode);
		else updateVisible(key, episode);
		if (confirmationOwed) confirmReconnectSave(key);
		episode.settled = true;
	};
	const resetEpisode = (key: string, episode: Episode) => {
		stopSavingTimer(episode);
		stopSavedTimer(episode);
		episodes.delete(key);
		if (visibleKey === key) current = 'idle';
	};

	$effect(() => {
		const { key, draft, activity = inactiveActivity } = snapshot();
		visibleKey = key;
		if (!key || !draft) {
			current = 'idle';
			return;
		}
		const clean = draft.editText === draft.savedText && !activity.writeInFlight && !activity.resaveQueued;
		let episode = episodes.get(key);
		if (draft.status === 'error') {
			if (episode) resetEpisode(key, episode);
			return;
		}
		if (activity.writeInFlight && (!episode || episode.settled)) {
			if (episode) stopSavedTimer(episode);
			episode = {
				startedAt: Date.now(), suspendedAt: null, showingSaving: false,
				savingTimer: null, savedTimer: null, showingSaved: false, settled: false,
			};
			episodes.set(key, episode);
			scheduleSaving(key, episode);
		}
		if (!episode) {
			current = 'idle';
			return;
		}
		if (episode.settled) {
			updateVisible(key, episode);
			return;
		}
		if (activity.awaitingReconnect) {
			if (episode.suspendedAt === null) episode.suspendedAt = Date.now();
			stopSavingTimer(episode);
			episode.showingSaving = false;
			updateVisible(key, episode);
			return;
		}
		if (episode.suspendedAt !== null) {
			episode.startedAt += Date.now() - episode.suspendedAt;
			episode.suspendedAt = null;
			scheduleSaving(key, episode);
		}
		if (clean) finishEpisode(key, episode, activity.confirmationOwed);
		else updateVisible(key, episode);
	});
	$effect(() => () => {
		for (const episode of episodes.values()) {
			stopSavingTimer(episode);
			stopSavedTimer(episode);
		}
	});
	return { get current(): DocSaveIndicator { return current; } };
}
