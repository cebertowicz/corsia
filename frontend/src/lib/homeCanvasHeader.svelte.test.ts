import { flushSync } from 'svelte';
import { get } from 'svelte/store';
import { afterEach, expect, it, vi } from 'vitest';
import { canvasHeader } from '$lib/stores/canvasHeader';
import { activityPhoneItem } from '$lib/stores/activityPhoneItem';
import { syncCanvasHeader } from '$lib/homeCanvasHeader';

afterEach(() => {
	activityPhoneItem.set(null);
	canvasHeader.set(null);
});

it('re-runs the effect that syncs the header when the phone Activity item changes', () => {
	const runs = vi.fn();
	const stop = $effect.root(() => {
		$effect(() => {
			runs();
			syncCanvasHeader({
				threadSettingsOpen: false, compactArtifact: null, contextAttachTarget: null, activeView: 'activity',
				uiScopeName: null, activeThreadId: null, conductorThreadId: null, threads: [],
				closeThreadSettings: () => {}, closeSettings: () => {}, closeDocuments: () => {},
			});
		});
	});
	flushSync();
	expect(get(canvasHeader)?.title).toBe('Activity');

	activityPhoneItem.set({ position: 3, count: 7, nextDisabled: false, nextTitle: 'Next one', onBack: () => {}, onNext: () => {} });
	flushSync();
	expect(get(canvasHeader)?.actions?.[0]?.label).toBe('3 of 7 · Next ›');

	activityPhoneItem.set(null);
	flushSync();
	expect(get(canvasHeader)?.title).toBe('Activity');
	expect(get(canvasHeader)?.actions).toBeUndefined();
	expect(runs).toHaveBeenCalledTimes(3);
	stop();
});
