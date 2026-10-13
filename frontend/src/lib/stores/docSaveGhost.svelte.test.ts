import { flushSync } from 'svelte';
import { afterEach, describe, expect, it, vi } from 'vitest';
import { docSaveGhost, type DocSaveCueSnapshot } from './docSaveGhost.svelte';
import type { DocSaveActivity, DocSaveDraft, DocSaveStatus } from './docSaveQueue';

const inactive: DocSaveActivity = {
	writeInFlight: false,
	resaveQueued: false,
	awaitingReconnect: false,
	confirmationOwed: false,
};

function draft(status: DocSaveStatus, editText = 'changed', savedText = 'base'): DocSaveDraft {
	return {
		target: { instanceId: 'tab-a', path: '/doc.md', name: 'doc.md', artifactId: 'artifact', projectId: null, threadId: null },
		editText,
		savedText,
		status,
		error: '',
		savedRevision: null,
	};
}

function cue(key: string | null, state: DocSaveDraft | undefined, activity: Partial<DocSaveActivity> = {}): DocSaveCueSnapshot {
	return { key, draft: state, activity: { ...inactive, ...activity } };
}

function mount(initial: DocSaveCueSnapshot) {
	let snapshot = $state(initial);
	let ghost!: ReturnType<typeof docSaveGhost>;
	const teardown = $effect.root(() => { ghost = docSaveGhost(() => snapshot); });
	flushSync();
	return {
		ghost,
		set(next: DocSaveCueSnapshot) { snapshot = next; flushSync(); },
		teardown,
	};
}

afterEach(() => vi.useRealTimers());

describe('docSaveGhost', () => {
	it('idle', () => {
		const view = mount(cue('tab-a', draft('idle', 'base', 'base')));
		expect(view.ghost.current).toBe('idle');
		view.teardown();
	});

	it('typing remains quiet during the debounce', () => {
		const view = mount(cue('tab-a', draft('dirty')));
		expect(view.ghost.current).toBe('idle');
		view.teardown();
	});

	it('stays quiet for an episode under 400ms', () => {
		vi.useFakeTimers();
		const view = mount(cue('tab-a', draft('saving'), { writeInFlight: true }));
		vi.advanceTimersByTime(399);
		expect(view.ghost.current).toBe('idle');
		view.set(cue('tab-a', draft('saved', 'changed', 'changed')));
		expect(view.ghost.current).toBe('idle');
		view.teardown();
	});

	it('shows Saving after an episode is 400ms old', () => {
		vi.useFakeTimers();
		const view = mount(cue('tab-a', draft('saving'), { writeInFlight: true }));
		vi.advanceTimersByTime(400);
		expect(view.ghost.current).toBe('saving');
		view.teardown();
	});

	it('holds Saving through a keystroke during a slow write', () => {
		vi.useFakeTimers();
		const view = mount(cue('tab-a', draft('saving'), { writeInFlight: true }));
		vi.advanceTimersByTime(400);
		view.set(cue('tab-a', draft('dirty', 'newer'), { writeInFlight: true, resaveQueued: true }));
		expect(view.ghost.current).toBe('saving');
		view.teardown();
	});

	it('holds one Saving cue through chained saves', () => {
		vi.useFakeTimers();
		const view = mount(cue('tab-a', draft('saving'), { writeInFlight: true }));
		vi.advanceTimersByTime(400);
		view.set(cue('tab-a', draft('dirty', 'newer'), { resaveQueued: true }));
		view.set(cue('tab-a', draft('saving', 'newer'), { writeInFlight: true }));
		expect(view.ghost.current).toBe('saving');
		view.set(cue('tab-a', draft('saved', 'newer', 'newer')));
		expect(view.ghost.current).toBe('saved');
		view.teardown();
	});

	it('shows Saved for 1400ms after a visible Saving cue ends cleanly', () => {
		vi.useFakeTimers();
		const view = mount(cue('tab-a', draft('saving'), { writeInFlight: true }));
		vi.advanceTimersByTime(400);
		view.set(cue('tab-a', draft('saved', 'changed', 'changed')));
		expect(view.ghost.current).toBe('saved');
		vi.advanceTimersByTime(1399);
		expect(view.ghost.current).toBe('saved');
		vi.advanceTimersByTime(1);
		expect(view.ghost.current).toBe('idle');
		view.teardown();
	});

	it('hides Saving while a live reconnect is pending and owes Saved', () => {
		vi.useFakeTimers();
		const view = mount(cue('tab-a', draft('saving'), { writeInFlight: true }));
		vi.advanceTimersByTime(400);
		view.set(cue('tab-a', draft('dirty'), { awaitingReconnect: true, confirmationOwed: true }));
		expect(view.ghost.current).toBe('idle');
		view.set(cue('tab-a', draft('saved', 'changed', 'changed'), { confirmationOwed: true }));
		expect(view.ghost.current).toBe('saved');
		view.teardown();
	});

	it('owes Saved when disconnecting before the 400ms threshold', () => {
		vi.useFakeTimers();
		const view = mount(cue('tab-a', draft('saving'), { writeInFlight: true }));
		vi.advanceTimersByTime(399);
		view.set(cue('tab-a', draft('dirty'), { awaitingReconnect: true, confirmationOwed: true }));
		expect(view.ghost.current).toBe('idle');
		view.set(cue('tab-a', draft('saved', 'changed', 'changed'), { confirmationOwed: true }));
		expect(view.ghost.current).toBe('saved');
		view.teardown();
	});

	it('shows owed Saved after a fast reconnect flush', () => {
		vi.useFakeTimers();
		const view = mount(cue('tab-a', draft('saving'), { writeInFlight: true }));
		view.set(cue('tab-a', draft('dirty'), { awaitingReconnect: true, confirmationOwed: true }));
		view.set(cue('tab-a', draft('saving'), { writeInFlight: true, confirmationOwed: true }));
		vi.advanceTimersByTime(30);
		view.set(cue('tab-a', draft('saved', 'changed', 'changed'), { confirmationOwed: true }));
		expect(view.ghost.current).toBe('saved');
		view.teardown();
	});

	it('ends an error episode without Saved', () => {
		vi.useFakeTimers();
		const view = mount(cue('tab-a', draft('saving'), { writeInFlight: true }));
		vi.advanceTimersByTime(400);
		view.set(cue('tab-a', draft('error')));
		expect(view.ghost.current).toBe('idle');
		view.teardown();
	});

	it('starts a new episode after an error and edit retry', () => {
		vi.useFakeTimers();
		const view = mount(cue('tab-a', draft('error')));
		view.set(cue('tab-a', draft('saving'), { writeInFlight: true }));
		vi.advanceTimersByTime(400);
		expect(view.ghost.current).toBe('saving');
		view.teardown();
	});

	it('does not carry a visible cue across a tab switch', () => {
		vi.useFakeTimers();
		const view = mount(cue('tab-a', draft('saving'), { writeInFlight: true }));
		vi.advanceTimersByTime(400);
		expect(view.ghost.current).toBe('saving');
		view.set(cue('tab-b', draft('idle', 'base', 'base')));
		expect(view.ghost.current).toBe('idle');
		view.teardown();
	});
});
