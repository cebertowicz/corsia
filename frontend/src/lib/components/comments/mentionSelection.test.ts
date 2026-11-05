import { describe, expect, it } from 'vitest';
import { createMentionSelection } from './mentionSelection';

describe('picker selections', () => {
	it('tracks the selected occurrence when identical text is independently typed and edited', () => {
		const tags = createMentionSelection('@A @A');
		tags.select('second', 3, '@A');
		tags.begin(0, 3, 'deleteContent'); tags.update('@A');
		expect(tags.occurrences('@A')).toEqual([{ agent_id: 'second', start: 0, end: 2 }]);
		tags.begin(0, 2, 'deleteContent'); tags.update('');
		expect(tags.ids('')).toEqual([]);
	});
	it('uses UTF-16 offsets and preserves identity across a recreated consumer', () => {
		const draft = { body: '😀 @Same', tags: createMentionSelection('😀 @Same') };
		draft.tags.select('agent-2', 3, '@Same');
		const remounted = draft.tags;
		remounted.begin(0, 0, 'insertText'); draft.body = 'x' + draft.body; remounted.update(draft.body);
		expect(remounted.occurrences(draft.body)).toEqual([{ agent_id: 'agent-2', start: 4, end: 9 }]);
	});
	it('removes identity when a selected mention is replaced by identical typed text', () => {
		const tags = createMentionSelection('@A'); tags.select('agent', 0, '@A');
		tags.begin(0, 2, 'insertText'); tags.update('@A');
		expect(tags.ids('@A')).toEqual([]);
	});
	it('restores an unambiguous selected identity on undo', () => {
		const tags = createMentionSelection('@A'); tags.select('agent', 0, '@A');
		tags.begin(2, 2, 'insertText'); tags.update('@A!');
		tags.begin(3, 3, 'historyUndo'); tags.update('@A');
		expect(tags.ids('@A')).toEqual(['agent']);
	});
	it('drops identities when an edit has no reliable range', () => {
		const tags = createMentionSelection('@A @A'); tags.select('second', 3, '@A');
		tags.update('@A'); expect(tags.ids('@A')).toEqual([]);
	});
});
