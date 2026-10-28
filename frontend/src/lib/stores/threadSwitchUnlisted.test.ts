import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { composerSelectionFor } from './composerSelection';
import { applyPendingThreadModelSwitch } from './threadModelSwitch';
import { unlistedThreadRows } from './threadRow';
import { activeThreadId, threads } from './threads';
import { handleSessionUpdated } from '../ws/handlersSession';

// The thread list omits the scope's conductor / CORSIA agent, so a pending switch on it has no list
// row to land on. Its switch state must still reach the composer that shows the switching icon.

const pendingBody = { operation_id: 'op-1', status: 'preparing', pending_model: 'model/target', checkpoint_required: true };

beforeEach(() => {
	activeThreadId.set('conductor');
	threads.set([{ id: 'chat', model: 'model/open' } as never]);
	unlistedThreadRows.set(new Map());
});

afterEach(() => {
	activeThreadId.set(null);
	threads.set([]);
	unlistedThreadRows.set(new Map());
});

describe('a pending switch on a thread the list omits', () => {
	it('reaches the composer from the 202 response', () => {
		applyPendingThreadModelSwitch('conductor', pendingBody);

		expect(get(composerSelectionFor)('conductor').switching)
			.toMatchObject({ pending_model: 'model/target', switch_status: 'preparing' });
	});

	it('is replaced by a failure frame and cleared by the commit frame', () => {
		applyPendingThreadModelSwitch('conductor', pendingBody);
		handleSessionUpdated({ thread_id: 'conductor', pending_model: 'model/target', switch_status: 'failed', switch_error: 'No room' });
		expect(get(composerSelectionFor)('conductor').switching)
			.toMatchObject({ switch_status: 'failed', switch_error: 'No room' });

		handleSessionUpdated({ thread_id: 'conductor', pending_model: null, switch_status: null, switch_error: null, switch_operation_id: null });
		expect(get(composerSelectionFor)('conductor').switching?.pending_model).toBeNull();
	});

	it('reaches the composer from a pending frame', () => {
		handleSessionUpdated({ thread_id: 'conductor', pending_model: 'model/target', switch_status: 'preparing' });

		expect(get(composerSelectionFor)('conductor').switching?.pending_model).toBe('model/target');
	});

	it('leaves a listed chat on its list row and keeps the unlisted store empty', () => {
		applyPendingThreadModelSwitch('chat', pendingBody);

		expect(get(threads)[0]).toMatchObject({ pending_model: 'model/target' });
		expect(get(composerSelectionFor)('chat').switching).toMatchObject({ pending_model: 'model/target' });
		expect(get(unlistedThreadRows).size).toBe(0);
	});
});
