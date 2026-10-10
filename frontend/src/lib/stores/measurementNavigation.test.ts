import { describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { measurementScope, startMeasurementUrlSync } from './measurement';
import { measurementGrain } from './measurementGrain';
import { measurementViewRequested } from './measurementAddress';
import {
	laneEntryScope,
	measurementCloseOrigin,
	measurementOrigin,
	openMeasurementFromProject,
	openMeasurementFromRail,
	measurementRailHref,
	openMeasurementFromThread,
} from './measurementNavigation';

describe('Measurement doorway scope (PL-2)', () => {
	it('opens an agent thread at that agent inside its project', () => {
		expect(laneEntryScope({ threadId: 'ag-1', agentId: 'ag-1', projectId: 'prj-a', isConductor: false }))
			.toEqual({ project: 'prj-a', agent: 'ag-1' });
	});

	it('opens a conductor thread at its project, never at the conductor agent', () => {
		expect(laneEntryScope({ threadId: 'conductor-1', agentId: 'conductor-1', projectId: 'prj-a', isConductor: true }))
			.toEqual({ project: 'prj-a', agent: null });
	});

	it('falls back to All work for a lane with no project instead of inventing one', () => {
		expect(laneEntryScope({ threadId: 'ag-1', agentId: 'ag-1', projectId: null, isConductor: false }))
			.toEqual({ project: 'all', agent: 'ag-1' });
	});

	it('preserves a thread doorway scope when the Measurement page starts URL synchronization', () => {
		window.history.replaceState(null, '', '/?project=all&agent=none&grain=days');
		openMeasurementFromThread({ threadId: 'ag-1', agentId: 'ag-1', projectId: 'prj-a', isConductor: false });
		const stop = startMeasurementUrlSync();
		expect(get(measurementScope)).toEqual({ project: 'prj-a', agent: 'ag-1' });
		stop();
	});
	it('keeps the scope across an overlay opening and closing on the same address', () => {
		// The app's back layer pushes a history entry with no URL argument, so the
		// address the reader is on is untouched; closing the overlay fires popstate.
		window.history.replaceState(null, '', '/?project=prj-a&agent=ag-1&grain=weeks');
		const stop = startMeasurementUrlSync();
		window.history.pushState({ backLayer: 7 }, '');
		window.dispatchEvent(new PopStateEvent('popstate', { state: null }));
		expect(get(measurementScope)).toEqual({ project: 'prj-a', agent: 'ag-1' });
		expect(get(measurementGrain)).toBe('weeks');
		stop();
	});

	it('follows the address when a route change really does drop the measurement keys', () => {
		// The reset a reviewer worried about only happens when the address itself no
		// longer names a scope, which is a route change, not an overlay. Pinned so the
		// boundary is visible instead of guessed.
		window.history.replaceState(null, '', '/?project=prj-a&agent=ag-1&grain=weeks');
		const stop = startMeasurementUrlSync();
		window.history.pushState(null, '', '/');
		window.dispatchEvent(new PopStateEvent('popstate', { state: null }));
		expect(get(measurementScope)).toEqual({ project: 'all', agent: null });
		expect(get(measurementGrain)).toBe('days');
		stop();
	});

	it('preserves project and conductor doorway scopes when Measurement hydrates', () => {
		window.history.replaceState(null, '', '/?project=all&agent=none&grain=days');
		openMeasurementFromProject('prj-b');
		let stop = startMeasurementUrlSync();
		expect(get(measurementScope)).toEqual({ project: 'prj-b', agent: null });
		stop();

		window.history.replaceState(null, '', '/?project=all&agent=none&grain=days');
		openMeasurementFromThread({ threadId: 'conductor-1', agentId: null, projectId: 'prj-c', isConductor: true });
		stop = startMeasurementUrlSync();
		expect(get(measurementScope)).toEqual({ project: 'prj-c', agent: null });
		stop();
	});

	it('records the thread doorway as the origin and leaves the project doorway on Activity', () => {
		openMeasurementFromThread({ threadId: 'ag-1', agentId: 'ag-1', projectId: 'prj-a', isConductor: false });
		expect(get(measurementOrigin)).toEqual({ kind: 'thread', threadId: 'ag-1' });
		expect(get(measurementScope)).toEqual({ project: 'prj-a', agent: 'ag-1' });
		expect(measurementCloseOrigin()).toEqual({ kind: 'thread', threadId: 'ag-1' });

		openMeasurementFromProject('prj-b');
		expect(get(measurementScope)).toEqual({ project: 'prj-b', agent: null });
		expect(measurementCloseOrigin()).toEqual({ kind: 'activity' });
	});

	it('opens the current project on a rail entry whatever scope was used last, and records no origin (PL-1)', () => {
		window.history.replaceState(null, '', '/');
		measurementScope.set({ project: 'prj-b', agent: 'ag-9' });
		openMeasurementFromRail('prj-a');
		expect(get(measurementScope)).toEqual({ project: 'prj-a', agent: null });
		expect(get(measurementOrigin)).toBeNull();
		expect(new URLSearchParams(window.location.search).get('project')).toBe('prj-a');
	});

	it('opens All work on a rail entry outside any project', () => {
		measurementScope.set({ project: 'prj-b', agent: 'ag-9' });
		openMeasurementFromRail(null);
		expect(get(measurementScope)).toEqual({ project: 'all', agent: null });
	});

	it('builds the rail href from the project alone', () => {
		expect(measurementRailHref('prj-a')).toBe('/?view=measurement&project=prj-a');
		expect(measurementRailHref(null)).toBe('/?view=measurement');
	});

	it('adopts the scope a reader arrived with in the address, when the page starts', () => {
		window.history.replaceState(null, '', '/?project=prj-c&agent=none&grain=weeks');
		measurementScope.set({ project: 'prj-a', agent: 'ag-1' });
		measurementGrain.set('days');
		const stop = startMeasurementUrlSync();
		expect(get(measurementScope)).toEqual({ project: 'prj-c', agent: null });
		expect(get(measurementGrain)).toBe('weeks');
		stop();
	});

	it('names the Measurement page in the address so a reload or shared link reopens it with its scope', () => {
		window.history.replaceState(null, '', '/?thread=abc');
		expect(measurementViewRequested()).toBe(false);
		openMeasurementFromThread({ threadId: 'ag-1', agentId: 'ag-1', projectId: 'prj-a', isConductor: false });
		const params = new URLSearchParams(window.location.search);
		expect(measurementViewRequested()).toBe(true);
		expect([params.get('view'), params.get('project'), params.get('agent'), params.get('thread')]).toEqual(['measurement', 'prj-a', 'ag-1', 'abc']);

		// A fresh load of that address hydrates the same scope in every section.
		measurementScope.set({ project: 'all', agent: null });
		const stop = startMeasurementUrlSync();
		expect(get(measurementScope)).toEqual({ project: 'prj-a', agent: 'ag-1' });

		// Leaving the page takes its keys out of the address and leaves the app's own.
		stop();
		const left = new URLSearchParams(window.location.search);
		expect([left.get('view'), left.get('project'), left.get('agent'), left.get('grain'), left.get('thread')]).toEqual([null, null, null, null, 'abc']);
		expect(measurementViewRequested()).toBe(false);
	});

	it('has a deterministic Close when nothing recorded an origin', () => {
		openMeasurementFromRail(null);
		expect(measurementCloseOrigin()).toBeNull();
		// Reading the origin clears it, so a later direct-URL entry cannot return to a stale thread.
		expect(measurementCloseOrigin()).toBeNull();
	});
});
