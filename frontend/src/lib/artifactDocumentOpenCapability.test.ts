import { describe, expect, it } from 'vitest';
import { documentOpenCapability } from './artifactDocumentOpenCapability';
import { emptyWorkspace, type ThreadArtifactWorkspace } from './artifactTabs';

const documentTab = { kind: 'document' as const, instanceId: 'a', artifactId: 'artifact-a', path: '/a.md', title: 'a.md', threadId: null, loadedRevision: null, latestRevision: null, status: 'ready' as const };

function workspace(overrides: Partial<ThreadArtifactWorkspace> = {}): ThreadArtifactWorkspace {
	return { ...emptyWorkspace(), tabs: [documentTab], activeTabId: 'a', visible: true, ...overrides };
}

describe('documentOpenCapability', () => {
	it('reports a split document as showing and unavailable', () => {
		expect(documentOpenCapability(workspace(), { mode: 'split', artifactWidth: 400 }, false)).toEqual({ showing: 'artifact-a', canShow: false });
	});

	it('allows a hidden rail leaf to be revealed', () => {
		expect(documentOpenCapability(workspace({ visible: false }), { mode: 'rail' }, false)).toEqual({ showing: null, canShow: true });
	});

	it('allows an empty desktop canvas even though its layout is chat-only', () => {
		expect(documentOpenCapability(emptyWorkspace(), { mode: 'chat-only' }, false)).toEqual({ showing: null, canShow: true });
	});

	it('keeps compact chat unavailable even with a chat-only layout', () => {
		expect(documentOpenCapability(emptyWorkspace(), { mode: 'chat-only' }, true)).toEqual({ showing: null, canShow: false });
	});

	it('reports a compact artifact as showing and unavailable', () => {
		expect(documentOpenCapability(workspace({ activeSurface: 'artifact' }), { mode: 'compact' }, true)).toEqual({ showing: 'artifact-a', canShow: false });
	});

	it('treats an unresolved or auxiliary visible desktop leaf as occupied', () => {
		const unresolved = workspace({ tabs: [{ ...documentTab, artifactId: null }] });
		const auxiliary = workspace({ tabs: [{ ...documentTab, kind: 'subagent', artifactId: null, threadId: 'child' }] });
		expect(documentOpenCapability(unresolved, { mode: 'split', artifactWidth: 400 }, false)).toEqual({ showing: null, canShow: false });
		expect(documentOpenCapability(auxiliary, { mode: 'split', artifactWidth: 400 }, false)).toEqual({ showing: null, canShow: false });
	});
});
