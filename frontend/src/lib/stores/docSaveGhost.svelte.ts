import { fromStore, derived, writable } from 'svelte/store';
import {
	confirmReconnectSave, docSaveActivities, docSaveDrafts, idleDocSaveActivity,
	type DocSaveActivity, type DocSaveDraft,
} from './docSaveQueue';

export type DocSaveIndicator = 'idle' | 'saving' | 'saved';
export interface DocSaveCueSnapshot {
	key: string | null;
	draft: DocSaveDraft | undefined;
	activity?: DocSaveActivity;
}
interface Episode {
	startedAt: number;
	suspendedAt: number | null;
	showedSaving: boolean;
	settled: boolean;
	timer: ReturnType<typeof setTimeout> | null;
}

const EPISODE_NOTICE_MS = 400;
const SAVED_NOTICE_MS = 1400;

/** One timer owner for every draft, independent of which viewer is mounted. */
export function createDocSaveCues(confirm: (key: string) => void) {
	const episodes = new Map<string, Episode>();
	const indicators = writable<Record<string, DocSaveIndicator>>({});
	const show = (key: string, value: DocSaveIndicator) => {
		indicators.update((all) => all[key] === value ? all : { ...all, [key]: value });
	};
	const stop = (episode: Episode) => {
		if (episode.timer !== null) clearTimeout(episode.timer);
		episode.timer = null;
	};
	const forget = (key: string, episode: Episode) => {
		stop(episode);
		episodes.delete(key);
		show(key, 'idle');
	};
	const scheduleSaving = (key: string, episode: Episode) => {
		stop(episode);
		episode.timer = setTimeout(() => {
			episode.timer = null;
			episode.showedSaving = true;
			show(key, 'saving');
		}, Math.max(0, EPISODE_NOTICE_MS - (Date.now() - episode.startedAt)));
	};
	const finish = (key: string, episode: Episode, owed: boolean) => {
		stop(episode);
		episode.settled = true;
		if (!episode.showedSaving && !owed) {
			forget(key, episode);
			return;
		}
		show(key, 'saved');
		episode.timer = setTimeout(() => forget(key, episode), SAVED_NOTICE_MS);
		if (owed) confirm(key);
	};
	const update = ({ key, draft, activity = idleDocSaveActivity }: DocSaveCueSnapshot) => {
		if (!key) return;
		let episode = episodes.get(key);
		if (!draft || draft.status === 'error') {
			if (episode) forget(key, episode);
			else show(key, 'idle');
			return;
		}
		const clean = draft.editText === draft.savedText && !activity.writeInFlight && !activity.resaveQueued;
		if (episode?.settled) {
			// A fresh edit dismisses the old badge; its save starts a fresh clock.
			if (!clean) forget(key, episode);
			else return;
			episode = undefined;
		}
		if (!episode && (activity.writeInFlight || activity.confirmationOwed)) {
			episode = { startedAt: Date.now(), suspendedAt: null, showedSaving: false, settled: false, timer: null };
			episodes.set(key, episode);
			scheduleSaving(key, episode);
		}
		if (!episode) return;
		if (activity.awaitingReconnect) {
			if (episode.suspendedAt === null) episode.suspendedAt = Date.now();
			stop(episode);
			show(key, 'idle');
			return;
		}
		if (episode.suspendedAt !== null) {
			episode.startedAt += Date.now() - episode.suspendedAt;
			episode.suspendedAt = null;
			scheduleSaving(key, episode);
		}
		if (clean) finish(key, episode, activity.confirmationOwed);
	};
	return {
		store: { subscribe: indicators.subscribe },
		update,
		prune(keys: Set<string>) {
			for (const [key, episode] of episodes) if (!keys.has(key)) forget(key, episode);
			indicators.update((all) => Object.fromEntries(Object.entries(all).filter(([key]) => keys.has(key))));
		},
		destroy() { for (const episode of episodes.values()) stop(episode); episodes.clear(); },
	};
}

const cues = createDocSaveCues(confirmReconnectSave);
// Observe every draft, including inactive tabs. Closing the draft removes its clock.
derived([docSaveDrafts, docSaveActivities], ([drafts, activities]) => ({ drafts, activities }))
	.subscribe(({ drafts, activities }) => {
		cues.prune(new Set(Object.keys(drafts)));
		for (const [key, draft] of Object.entries(drafts)) cues.update({ key, draft, activity: activities[key] });
	});

/** A tab switch never replays a confirmation that already happened offscreen. */
export function docSaveGhost(key: () => string | null) {
	const indicators = fromStore(cues.store);
	let watched = $state<string | null>(null);
	let showSaved = $state(false);
	let previous: DocSaveIndicator = 'idle';
	$effect(() => {
		const now = key();
		const value = now ? indicators.current[now] ?? 'idle' : 'idle';
		showSaved = now === watched && value === 'saved' && (showSaved || previous !== 'saved');
		watched = now;
		previous = value;
	});
	return {
		get current(): DocSaveIndicator {
			const now = key();
			const value = now ? indicators.current[now] ?? 'idle' : 'idle';
			return value === 'saved' ? (now === watched && showSaved ? 'saved' : 'idle') : value;
		},
	};
}
