import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';
import { openArtifactTab, threadWorkspaces, closeArtifactTab, setLeafVisible, setActiveTab, setActiveSurface } from '../stores/artifactWorkspace';
import { registerDocumentViewport } from '../stores/documentViewport';
import { subagentMeta, type SubagentRef } from '../stores/subagents';
import { setWs } from '../ws/state';
import { sendMessage } from '../ws/send';

let sent: Record<string, unknown>[];
beforeEach(() => {
	threadWorkspaces.set({});
	subagentMeta.set(new Map());
	sent = [];
	setWs({ readyState: WebSocket.OPEN, send: (data: string) => sent.push(JSON.parse(data)) } as unknown as WebSocket);
});

function open(threadId: string, artifactId: string, heading: number | null = null) {
	openArtifactTab(threadId, { artifactId, path: `${artifactId}.md`, title: artifactId });
	const instanceId = get(threadWorkspaces)[threadId].activeTabId!;
	const release = registerDocumentViewport(instanceId, () => (heading == null ? null : { anchor: heading, line: `## Line ${heading}` }));
	return { instanceId, release };
}

describe('DCOL-FR9 viewing document send payload', () => {
	it('sends only the active document in the receiving thread, with its current viewport heading', () => {
		const other = open('other', 'other-doc', 2);
		const first = open('main', 'first', 3);
		const latest = open('main', 'latest', 12);
		expect(sent).toHaveLength(0); // Opening/scrolling never sends a wake.
		sendMessage('main', 'What about this?');
		expect(sent.at(-1)?.viewing_document).toEqual({ artifact_id: 'latest', heading_anchor: 12, heading_line: '## Line 12' });
		setActiveTab('main', first.instanceId);
		sendMessage('main', 'Back to the first');
		expect(sent.at(-1)?.viewing_document).toEqual({ artifact_id: 'first', heading_anchor: 3, heading_line: '## Line 3' });
		other.release(); first.release(); latest.release();
	});

	it('omits the note when closed, hidden, or sending to a different thread', () => {
		const doc = open('main', 'design');
		sendMessage('other', 'No canvas here');
		expect(sent.at(-1)).not.toHaveProperty('viewing_document');
		setLeafVisible('main', false);
		sendMessage('main', 'Hidden');
		expect(sent.at(-1)).not.toHaveProperty('viewing_document');
		closeArtifactTab('main', doc.instanceId);
		sendMessage('main', 'Closed');
		expect(sent.at(-1)).not.toHaveProperty('viewing_document');
		doc.release();
	});

	it('still names the open document once typing in the composer has made chat the active surface', () => {
		const doc = open('main', 'design', 4);
		// Clicking or typing in the composer does this on every layout, before any send.
		setActiveSurface('main', 'chat');
		sendMessage('main', 'Asked from the composer');
		expect(sent.at(-1)?.viewing_document).toEqual({ artifact_id: 'design', heading_anchor: 4, heading_line: '## Line 4' });
		doc.release();
	});

	it('sends a document without a section and uses the same contract for a child composer', () => {
		const doc = open('child', 'design');
		subagentMeta.set(new Map([['child', { childThreadId: 'child' } as SubagentRef]]));
		sendMessage('child', 'At the top');
		expect(sent.at(-1)).toMatchObject({ type: 'subagent_feed', thread_id: 'child', viewing_document: { artifact_id: 'design' } });
		expect(sent.at(-1)?.viewing_document).not.toHaveProperty('heading_anchor');
		doc.release();
	});
});
