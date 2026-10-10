import { describe, expect, it } from 'vitest';
import { createOpenTiming, messageCountBucket, type OpenTimingSink } from './openTiming';

interface Recorded {
	name: string;
	properties: Record<string, unknown>;
}

function harness() {
	const events: Recorded[] = [];
	let clock = 0;
	const sink: OpenTimingSink = {
		now: () => clock,
		emit: (category, action, properties) => events.push({ name: `${category}.${action}`, properties }),
	};
	const timing = createOpenTiming(sink);
	return {
		timing,
		events,
		advance: (ms: number) => { clock += ms; },
	};
}

function last(events: Recorded[]): Recorded {
	return events[events.length - 1];
}

describe('messageCountBucket', () => {
	it('buckets the loaded count, treating an unknown or empty load as 0', () => {
		expect(messageCountBucket(0)).toBe('0');
		expect(messageCountBucket(-3)).toBe('0');
		expect(messageCountBucket(Number.NaN)).toBe('0');
		expect(messageCountBucket(1)).toBe('1-50');
		expect(messageCountBucket(50)).toBe('1-50');
		expect(messageCountBucket(51)).toBe('51-500');
		expect(messageCountBucket(500)).toBe('51-500');
		expect(messageCountBucket(501)).toBe('500+');
	});
});

describe('thread_open', () => {
	it('settles only once BOTH the first page and the artifact list are in', () => {
		const { timing, events, advance } = harness();
		timing.beginThreadOpen('t1', 'sidebar');

		advance(120);
		timing.markFirstPageRendered('t1', 12, true);
		expect(events).toEqual([]);

		advance(80);
		timing.markArtifactsLoaded('t1');

		expect(events).toEqual([{
			name: 'navigation.thread_open',
			properties: {
				click_to_first_page_ms: 120,
				click_to_settled_ms: 200,
				outcome: 'settled',
				source: 'sidebar',
				artifacts_awaited: true,
				message_count_bucket: '1-50',
			},
		}]);
	});

	it('settles when the artifact list resolves before the first page', () => {
		const { timing, events, advance } = harness();
		timing.beginThreadOpen('t1', 'link');

		advance(30);
		timing.markArtifactsLoaded('t1');
		expect(events).toEqual([]);

		advance(90);
		timing.markFirstPageRendered('t1', 0, true);

		expect(events).toHaveLength(1);
		expect(last(events).properties).toMatchObject({
			click_to_first_page_ms: 120,
			click_to_settled_ms: 120,
			outcome: 'settled',
			source: 'link',
			message_count_bucket: '0',
		});
	});

	// A thread whose artifacts section is collapsed, excluded by the scope's pane
	// config, or replaced by the document detail view mounts no pane at all. Nothing
	// on screen can report its list, so the first page IS the usable moment (#2280).
	it('settles on the first page when no artifact pane is on screen', () => {
		const { timing, events, advance } = harness();
		timing.beginThreadOpen('t1', 'sidebar');

		advance(140);
		timing.markFirstPageRendered('t1', 8, timing.artifactsPaneMounted('t1'));

		expect(events).toHaveLength(1);
		expect(last(events).properties).toEqual({
			click_to_first_page_ms: 140,
			click_to_settled_ms: 140,
			outcome: 'settled',
			source: 'sidebar',
			artifacts_awaited: false,
			message_count_bucket: '1-50',
		});
	});

	it('waits for the artifact list while a pane is on screen', () => {
		const { timing, events, advance } = harness();
		timing.beginThreadOpen('t1', 'sidebar');
		const release = timing.registerArtifactsPane('t1');

		advance(140);
		timing.markFirstPageRendered('t1', 8, timing.artifactsPaneMounted('t1'));
		expect(events).toEqual([]);

		advance(60);
		timing.markArtifactsLoaded('t1');
		expect(last(events).properties).toMatchObject({
			click_to_first_page_ms: 140,
			click_to_settled_ms: 200,
			outcome: 'settled',
			artifacts_awaited: true,
		});

		release();
		expect(events).toHaveLength(1);
	});

	it('waits for a pane that mounts while the open is still pending', () => {
		const { timing, events, advance } = harness();
		timing.beginThreadOpen('t1', 'link');

		advance(50);
		const release = timing.registerArtifactsPane('t1');
		advance(90);
		timing.markFirstPageRendered('t1', 3, timing.artifactsPaneMounted('t1'));
		expect(events).toEqual([]);

		advance(10);
		timing.markArtifactsLoaded('t1');
		expect(last(events).properties).toMatchObject({
			click_to_first_page_ms: 140,
			click_to_settled_ms: 150,
			outcome: 'settled',
		});
		release();
	});

	it('settles on the first page when the pane unmounts while the open is pending', () => {
		const { timing, events, advance } = harness();
		timing.beginThreadOpen('t1', 'sidebar');
		const release = timing.registerArtifactsPane('t1');

		advance(140);
		timing.markFirstPageRendered('t1', 8, timing.artifactsPaneMounted('t1'));
		expect(events).toEqual([]);

		advance(25);
		release();
		expect(events).toHaveLength(1);
		expect(last(events).properties).toEqual({
			click_to_first_page_ms: 140,
			click_to_settled_ms: 165,
			outcome: 'settled',
			source: 'sidebar',
			// The open did wait; the pane that owed the list left before it arrived.
			artifacts_awaited: true,
			message_count_bucket: '1-50',
		});
	});

	it('still settles on the first page when the pane left before it rendered', () => {
		const { timing, events, advance } = harness();
		timing.beginThreadOpen('t1', 'other');
		const release = timing.registerArtifactsPane('t1');
		release();

		advance(90);
		timing.markFirstPageRendered('t1', 0, timing.artifactsPaneMounted('t1'));
		expect(events).toHaveLength(1);
		expect(last(events).properties).toMatchObject({
			click_to_first_page_ms: 90,
			click_to_settled_ms: 90,
			outcome: 'settled',
		});
	});

	// Otherwise the settled percentile mixes two populations: an open that waited on a
	// pane's list, and one that never could have. This property is what separates them.
	it('reports whether the open waited on an artifact list, on both settles', () => {
		const paneLess = harness();
		paneLess.timing.beginThreadOpen('t1', 'link');
		paneLess.advance(70);
		paneLess.timing.markFirstPageRendered('t1', 5, false);
		expect(last(paneLess.events).properties.artifacts_awaited).toBe(false);

		const withPane = harness();
		withPane.timing.beginThreadOpen('t1', 'sidebar');
		withPane.timing.registerArtifactsPane('t1');
		withPane.advance(70);
		withPane.timing.markFirstPageRendered('t1', 5, true);
		withPane.advance(30);
		withPane.timing.markArtifactsLoaded('t1');

		expect(withPane.events).toHaveLength(1);
		expect(last(withPane.events).properties.artifacts_awaited).toBe(true);
	});

	// The route begins the open before its awaits and the panel begins it again once
	// the selection is real; only the first may decide when the open began (#2280).
	it('keeps the first start when the same open is begun twice', () => {
		const { timing, events, advance } = harness();
		timing.beginThreadOpen('t1', 'sidebar');

		advance(40);
		timing.beginThreadOpen('t1', 'other');
		advance(80);
		timing.markFirstPageRendered('t1', 2, false);

		expect(events).toHaveLength(1);
		expect(last(events).properties).toMatchObject({
			click_to_first_page_ms: 120,
			source: 'sidebar',
		});
	});

	it('emits exactly once however many reactive re-runs mark the same open', () => {
		const { timing, events } = harness();
		timing.beginThreadOpen('t1', 'sidebar');
		timing.markFirstPageRendered('t1', 4, true);
		timing.markArtifactsLoaded('t1');
		timing.markArtifactsLoaded('t1');
		timing.markFirstPageRendered('t1', 4, true);
		timing.failThreadOpen('t1');
		timing.noteDisplayedThread(null);

		expect(events).toHaveLength(1);
		expect(last(events).properties.outcome).toBe('settled');
	});

	it('ignores marks that belong to another thread', () => {
		const { timing, events } = harness();
		timing.beginThreadOpen('t1', 'sidebar');
		timing.markFirstPageRendered('other', 4, true);
		timing.markArtifactsLoaded('other');
		expect(events).toEqual([]);

		timing.markFirstPageRendered('t1', 4, true);
		timing.markArtifactsLoaded('t1');
		expect(events).toHaveLength(1);
	});

	it('emits abandoned with the times that exist when another thread is selected', () => {
		const { timing, events, advance } = harness();
		timing.beginThreadOpen('t1', 'sidebar');
		advance(140);
		timing.markFirstPageRendered('t1', 75, true);

		timing.beginThreadOpen('t2', 'sidebar');

		expect(events).toHaveLength(1);
		expect(last(events).properties).toEqual({
			click_to_first_page_ms: 140,
			click_to_settled_ms: null,
			outcome: 'abandoned',
			source: 'sidebar',
			artifacts_awaited: true,
			message_count_bucket: '51-500',
		});
	});

	it('emits abandoned with null times when nothing had rendered yet', () => {
		const { timing, events } = harness();
		timing.beginThreadOpen('t1', 'history');
		timing.noteDisplayedThread(null);

		expect(last(events).properties).toEqual({
			click_to_first_page_ms: null,
			click_to_settled_ms: null,
			outcome: 'abandoned',
			source: 'history',
			artifacts_awaited: false,
			message_count_bucket: '0',
		});
	});

	it('emits error when a load fails, and settles nothing afterwards', () => {
		const { timing, events } = harness();
		timing.beginThreadOpen('t1', 'other');
		timing.failThreadOpen('t1');
		timing.markFirstPageRendered('t1', 3, true);
		timing.markArtifactsLoaded('t1');

		expect(events).toHaveLength(1);
		expect(last(events).properties).toEqual({
			click_to_first_page_ms: null,
			click_to_settled_ms: null,
			outcome: 'error',
			source: 'other',
			artifacts_awaited: false,
			message_count_bucket: '0',
		});
	});

	it('ignores a failure reported for a thread that is not open', () => {
		const { timing, events } = harness();
		timing.beginThreadOpen('t1', 'other');
		timing.failThreadOpen('t2');
		timing.markFirstPageRendered('t1', 1, true);
		timing.markArtifactsLoaded('t1');
		expect(last(events).properties.outcome).toBe('settled');
	});

	// A realtime artifacts_changed refresh re-runs the same pane load. Success or
	// failure, it arrives after the open it belonged to is already over.
	it('emits nothing when a pane reports again after the open settled', () => {
		const { timing, events } = harness();
		timing.beginThreadOpen('t1', 'sidebar');
		const release = timing.registerArtifactsPane('t1');
		timing.markFirstPageRendered('t1', 3, timing.artifactsPaneMounted('t1'));
		timing.markArtifactsLoaded('t1');
		expect(events).toHaveLength(1);

		timing.markArtifactsLoaded('t1');
		timing.failThreadOpen('t1');
		release();

		expect(events).toHaveLength(1);
		expect(last(events).properties.outcome).toBe('settled');
	});

	it('abandons on a displayed-thread change, never on the same thread', () => {
		const { timing, events } = harness();
		timing.beginThreadOpen('t1', 'sidebar');
		timing.noteDisplayedThread('t1');
		expect(events).toEqual([]);

		timing.noteDisplayedThread('t2');
		expect(events).toHaveLength(1);
		expect(last(events).properties).toMatchObject({ outcome: 'abandoned', source: 'sidebar' });
	});

	it('is inert without an open', () => {
		const { timing, events } = harness();
		timing.markFirstPageRendered('t1', 5, false);
		timing.markArtifactsLoaded('t1');
		timing.failThreadOpen('t1');
		timing.noteDisplayedThread(null);
		expect(events).toEqual([]);
	});
});

describe('artifact_open', () => {
	it('settles with the kind and the click-to-content time', () => {
		const { timing, events, advance } = harness();
		timing.beginArtifactOpen('tab-1', 'document');
		advance(260);
		timing.markArtifactRendered('tab-1');

		expect(events).toEqual([{
			name: 'navigation.artifact_open',
			properties: { click_to_content_ms: 260, outcome: 'settled', kind: 'document' },
		}]);
	});

	it('emits once for a tab, whichever signal arrives first', () => {
		const { timing, events } = harness();
		timing.beginArtifactOpen('tab-1', 'document');
		timing.markArtifactRendered('tab-1');
		timing.markArtifactRendered('tab-1');
		timing.failArtifactOpen('tab-1');
		expect(events).toHaveLength(1);
	});

	it('ignores marks for another tab instance', () => {
		const { timing, events } = harness();
		timing.beginArtifactOpen('tab-1', 'document');
		timing.markArtifactRendered('tab-2');
		expect(events).toEqual([]);
	});

	it('emits error for a failed load and abandons a superseded open', () => {
		const { timing, events } = harness();
		timing.beginArtifactOpen('tab-1', 'document');
		timing.failArtifactOpen('tab-1');
		expect(last(events).properties).toEqual({
			click_to_content_ms: null, outcome: 'error', kind: 'document',
		});

		timing.beginArtifactOpen('tab-2', 'document');
		timing.beginArtifactOpen('tab-3', 'document');
		expect(last(events).properties).toEqual({
			click_to_content_ms: null, outcome: 'abandoned', kind: 'document',
		});
	});

	it('can be abandoned explicitly, and stays inert afterwards', () => {
		const { timing, events } = harness();
		timing.beginArtifactOpen('tab-1', 'document');
		timing.abandonArtifactOpen();
		timing.markArtifactRendered('tab-1');
		expect(events).toHaveLength(1);
		expect(last(events).properties.outcome).toBe('abandoned');
	});
});