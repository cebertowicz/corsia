import { describe, expect, it } from 'vitest';
import { createMentionSelection, typedMentions } from './mentionSelection';

describe('typed names', () => {
	const roster = [{ id: 'int', name: 'Integration' }, { id: 'sub', name: 'Agent Substrate' }, { id: 'eco', name: 'Ecosystem' }, { id: 'agent', name: 'Agent' }];
	it('tags every typed @name that names one agent, longest name first', () => {
		const text = 'Please look. @Integration @Agent Substrate @Ecosystem';
		expect(typedMentions(text, roster, [])).toEqual([
			{ agent_id: 'int', start: 13, end: 25 }, { agent_id: 'sub', start: 26, end: 42 }, { agent_id: 'eco', start: 43, end: 53 },
		]);
	});
	it('matches case-insensitively and stops at punctuation, but not inside a longer word or an address', () => {
		expect(typedMentions('@integration, thanks', roster, [])).toEqual([{ agent_id: 'int', start: 0, end: 12 }]);
		expect(typedMentions('@Integrations', roster, [])).toEqual([]);
		expect(typedMentions('mail me@Integration', roster, [])).toEqual([]);
	});
	it('leaves picked occurrences alone and skips a name two agents share', () => {
		const picked = [{ agent_id: 'sub', start: 0, end: 16 }];
		expect(typedMentions('@Agent Substrate', roster, picked)).toEqual([]);
		expect(typedMentions('@Twin', [{ id: 'a', name: 'Twin' }, { id: 'b', name: 'Twin' }], [])).toEqual([]);
	});
});

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
	it('restores identity on undo in a long draft, matching the text by its key rather than a stored copy', () => {
		const tags = createMentionSelection('@A'); tags.select('agent', 0, '@A');
		let text = '@A';
		for (let index = 0; index < 2000; index++) { tags.begin(text.length, text.length, 'insertText'); text += 'x'; tags.update(text); }
		const before = text;
		tags.begin(text.length, text.length, 'insertText'); tags.update(`${text}!`);
		tags.begin(text.length + 1, text.length + 1, 'historyUndo'); tags.update(before);
		expect(tags.ids(before)).toEqual(['agent']);
		// Same length, different text: nothing was recorded for it, so no identity is guessed.
		tags.begin(0, 0, 'historyRedo'); tags.update(`${before.slice(0, -1)}y`);
		expect(tags.ids(`${before.slice(0, -1)}y`)).toEqual([]);
	});
	it('drops identities when an edit has no reliable range', () => {
		const tags = createMentionSelection('@A @A'); tags.select('second', 3, '@A');
		tags.update('@A'); expect(tags.ids('@A')).toEqual([]);
	});
});
