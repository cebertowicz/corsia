import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import {
	applyTabLoadSignal, openArtifactTab, threadWorkspaces,
} from './artifactWorkspace';
import {
	clearDraft, docSaveDrafts, seedDraft, setDraftText,
} from './docSaveQueue';

describe('artifact workspace draft-safe dedupe', () => {
	beforeEach(() => {
		threadWorkspaces.set({});
		docSaveDrafts.set({});
	});

	it('keeps both instances when identity enrichment would discard an unsaved draft', () => {
		openArtifactTab('thread-1', { artifactId: 'art-1', path: '/docs/a.md', title: 'a' });
		openArtifactTab('thread-1', { path: '/alias/a.md', title: 'a alias' });
		const [survivor, duplicate] = get(threadWorkspaces)['thread-1'].tabs;

		seedDraft({
			instanceId: duplicate.instanceId, path: duplicate.path, name: duplicate.title,
			projectId: null, threadId: 'thread-1', artifactId: null,
		}, 'saved');
		setDraftText({ instanceId: duplicate.instanceId }, 'unsaved');
		applyTabLoadSignal('thread-1', {
			instanceId: duplicate.instanceId, status: 'ready', revision: 'r1', artifactId: 'art-1',
		});

		const tabs = get(threadWorkspaces)['thread-1'].tabs;
		expect(tabs).toHaveLength(2);
		expect(tabs.map((tab) => tab.instanceId)).toEqual([survivor.instanceId, duplicate.instanceId]);
		expect(get(docSaveDrafts)[duplicate.instanceId]?.editText).toBe('unsaved');

		clearDraft({ instanceId: duplicate.instanceId });
	});
});
