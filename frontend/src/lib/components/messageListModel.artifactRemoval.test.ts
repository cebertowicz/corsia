import { describe, it, expect } from 'vitest';
import {
	artifactRemovalEventLabel,
	buildVisibleMessages,
	groupArtifactRemovalEvents,
	isArtifactRemovalEvent,
	summarizeArtifactRemovalEvents,
} from './messageListModel';

function removalEvent(
	id: string,
	overrides: Record<string, unknown> = {},
): Record<string, unknown> {
	return {
		id,
		thread_id: 'sid',
		role: 'system',
		content: `Removed ${id}.md`,
		created_at: `2026-08-15T00:00:0${id}Z`,
		metadata: {
			type: 'artifact_removed',
			event_type: 'artifact.hidden_from_pane',
			subtype: 'hidden_from_pane',
			artifact_id: `art-${id}`,
			artifact_type: 'document',
			title: `${id}.md`,
			actor_kind: 'agent',
			causal_event_id: null,
			...overrides,
		},
	};
}

describe('isArtifactRemovalEvent', () => {
	it('recognizes the canonical removal message type', () => {
		expect(isArtifactRemovalEvent(removalEvent('1'))).toBe(true);
	});

	it('rejects a non-removal system message', () => {
		expect(isArtifactRemovalEvent({
			role: 'system',
			metadata: { type: 'context_event' },
		})).toBe(false);
	});

	it('rejects a user-role message even with matching metadata', () => {
		expect(isArtifactRemovalEvent({
			role: 'user',
			metadata: { type: 'artifact_removed' },
		})).toBe(false);
	});
});

describe('artifactRemovalEventLabel', () => {
	it('renders the raw message content for one ungrouped event', () => {
		expect(artifactRemovalEventLabel(removalEvent('1'))).toBe('Removed 1.md');
	});
});

describe('summarizeArtifactRemovalEvents', () => {
	it('returns the single event label for a batch of one', () => {
		const group = [removalEvent('1')];
		expect(summarizeArtifactRemovalEvents(group)).toBe('Removed 1.md');
	});

	it('names up to two titles and counts the rest', () => {
		const group = [
			removalEvent('1', { title: 'alpha.md' }),
			removalEvent('2', { title: 'beta.md' }),
			removalEvent('3', { title: 'gamma.md' }),
		];
		expect(summarizeArtifactRemovalEvents(group)).toBe('Removed alpha.md, beta.md +1 more');
	});

	it('labels a grouped shown_in_pane batch as restored', () => {
		const group = [
			removalEvent('1', {
				title: 'alpha.md',
				subtype: 'shown_in_pane',
				content: 'Restored alpha.md to pane',
			}),
			removalEvent('2', {
				title: 'beta.md',
				subtype: 'shown_in_pane',
				content: 'Restored beta.md to pane',
			}),
		];
		expect(summarizeArtifactRemovalEvents(group)).toBe('Restored alpha.md, beta.md');
	});

	it('uses a neutral label when one causal batch mixes subtypes', () => {
		const group = [
			removalEvent('1', { title: 'alpha.md', subtype: 'hidden_from_pane' }),
			removalEvent('2', { title: 'beta.md', subtype: 'shown_in_pane' }),
		];
		expect(summarizeArtifactRemovalEvents(group)).toBe('2 pane changes');
	});

	it('falls back to a bare count when no titles are present', () => {
		const group = [
			removalEvent('1', { title: null }),
			removalEvent('2', { title: null }),
		];
		expect(summarizeArtifactRemovalEvents(group)).toBe('2 artifacts removed');
	});
});

describe('groupArtifactRemovalEvents', () => {
	it('collapses consecutive events sharing one causal batch id into one row', () => {
		const rows = [
			removalEvent('1', { causal_event_id: 'turn-a', title: 'alpha.md' }),
			removalEvent('2', { causal_event_id: 'turn-a', title: 'beta.md' }),
		];
		const result = groupArtifactRemovalEvents(rows);
		expect(result).toHaveLength(1);
		expect(result[0].metadata.type).toBe('artifact_removed');
		expect(result[0].metadata.causal_event_id).toBe('turn-a');
		expect(result[0].metadata.removal_events).toHaveLength(2);
		expect(result[0].content).toBe('Removed alpha.md, beta.md');
	});

	it('never groups two events with no causal_event_id, even if adjacent', () => {
		const rows = [
			removalEvent('1', { causal_event_id: null }),
			removalEvent('2', { causal_event_id: null }),
		];
		const result = groupArtifactRemovalEvents(rows);
		expect(result).toHaveLength(2);
		expect(result[0].id).toBe('1');
		expect(result[1].id).toBe('2');
	});

	it('never groups events with different causal_event_id values', () => {
		const rows = [
			removalEvent('1', { causal_event_id: 'turn-a' }),
			removalEvent('2', { causal_event_id: 'turn-b' }),
		];
		const result = groupArtifactRemovalEvents(rows);
		expect(result).toHaveLength(2);
	});

	it('does not bridge across a non-removal message between two batch members', () => {
		const rows = [
			removalEvent('1', { causal_event_id: 'turn-a' }),
			{ id: 'mid', thread_id: 'sid', role: 'assistant', content: 'unrelated', created_at: 't', metadata: {} },
			removalEvent('2', { causal_event_id: 'turn-a' }),
		];
		const result = groupArtifactRemovalEvents(rows);
		expect(result).toHaveLength(3);
	});

	it('leaves non-removal messages untouched and in order', () => {
		const rows = [
			{ id: 'a', thread_id: 'sid', role: 'user', content: 'hi', created_at: 't0', metadata: {} },
			removalEvent('1', { causal_event_id: 'turn-a' }),
			removalEvent('2', { causal_event_id: 'turn-a' }),
			{ id: 'b', thread_id: 'sid', role: 'assistant', content: 'ok', created_at: 't3', metadata: {} },
		];
		const result = groupArtifactRemovalEvents(rows);
		expect(result.map((m) => m.id)).toEqual(['a', 'artifact-removal-digest-1-2', 'b']);
	});
});

describe('buildVisibleMessages with artifact removal events', () => {
	it('applies removal-event grouping alongside existing seed/routed-event filtering', () => {
		const rows = [
			{ id: 'seed', thread_id: 'sid', role: 'system', content: 'seed', created_at: 't0', metadata: { type: 'seed' } },
			removalEvent('1', { causal_event_id: 'turn-a' }),
			removalEvent('2', { causal_event_id: 'turn-a' }),
		];
		const result = buildVisibleMessages(rows);
		expect(result.map((m) => m.id)).toEqual(['artifact-removal-digest-1-2']);
	});
});
