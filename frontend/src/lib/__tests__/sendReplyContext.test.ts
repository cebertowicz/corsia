import { afterEach, beforeEach, describe, expect, it } from 'vitest';
import { beginComposing, cancelComposing, setVisibleRows } from '../stores/replyContextCapture';
import { subagentMeta, type SubagentRef } from '../stores/subagents';
import { visitDividers } from '../stores/threadUpdateCards';
import { setWs } from '../ws/state';
import { sendMessage } from '../ws/send';

function rows(threadId: string, ...seqs: number[]) {
	return seqs.map((seq) => ({ threadId, seq }));
}

let sent: Record<string, unknown>[];
beforeEach(() => {
	subagentMeta.set(new Map());
	visitDividers.set({});
	sent = [];
	setWs({ readyState: WebSocket.OPEN, send: (data: string) => sent.push(JSON.parse(data)) } as unknown as WebSocket);
});

afterEach(() => {
	cancelComposing();
	setVisibleRows(rows('main'));
	visitDividers.set({});
});

describe('reply context send payload', () => {
	it('sends what was in view while composing and the first unread message', () => {
		visitDividers.set({ main: { divider: { since: 's', first_seq: 4 }, failed: false } });
		setVisibleRows(rows('main', 3, 4, 5));
		beginComposing('main');
		sendMessage('main', 'Do the second one');
		expect(sent.at(-1)).toMatchObject({
			type: 'message', thread_id: 'main',
			reply_context: { in_view: [[3, 5, 3]], first_unread: { seq: 4, truncated: false } },
		});
	});

	it('sends comments in the same context as the view and unread position', () => {
		const comment = { id: 'c1', seq: 3, from_off: 2, to_off: 5, quote: 'one', anchor_prefix: 'a ', anchor_suffix: ' b', note: 'note' };
		visitDividers.set({ main: { divider: { since: 's', first_seq: 4 }, failed: false } });
		setVisibleRows(rows('main', 3, 4));
		beginComposing('main');
		sendMessage('main', 'answer', undefined, undefined, undefined, 'apply_now', [comment]);
		expect(sent.at(-1)?.reply_context).toEqual({ comments: [comment], in_view: [[3, 4, 2]], first_unread: { seq: 4, truncated: false } });
	});

	it('sends the window once: the next message starts from nothing', () => {
		setVisibleRows(rows('main', 3));
		beginComposing('main');
		sendMessage('main', 'first');
		sendMessage('main', 'second');
		expect(sent[0]).toHaveProperty('reply_context');
		expect(sent[1]).not.toHaveProperty('reply_context');
	});

	it('sends no field when there is nothing to carry', () => {
		sendMessage('main', 'plain');
		expect(sent.at(-1)).not.toHaveProperty('reply_context');
	});

	it('never rides the subagent feed', () => {
		subagentMeta.set(new Map([['child', { childThreadId: 'child' } as SubagentRef]]));
		visitDividers.set({ child: { divider: { since: 's', first_seq: 1 }, failed: false } });
		sendMessage('child', 'hi');
		expect(sent.at(-1)).toMatchObject({ type: 'subagent_feed' });
		expect(sent.at(-1)).not.toHaveProperty('reply_context');
	});
});
