import { get } from 'svelte/store';
import { afterEach, beforeEach, describe, expect, it } from 'vitest';

import { composerSelectionFor } from './composerSelection';
import { _resetThreadModelTruthForTests } from './models';
import { applySwitchRead, beginSwitchRead, heldThreadSwitch } from './threadModelTruth';
import { applyPendingThreadModelSwitch } from './threadModelSwitch';
import { activeThreadId, threads } from './threads';
import { handleSessionUpdated } from '../ws/handlersSession';

// The thread list omits the scope's conductor / CORSIA agent, so a pending switch on it has no list
// row to land on. Its switch state is held beside its model and must still reach the composer that
// shows the switching icon.

const pendingBody = { operation_id: 'op-1', status: 'preparing', pending_model: 'model/target', checkpoint_required: true };

beforeEach(() => {
	activeThreadId.set('conductor');
	threads.set([{ id: 'chat', model: 'model/open' } as never]);
	_resetThreadModelTruthForTests();
	// The page has read the conductor's target, so its switch state exists.
	applySwitchRead('conductor', beginSwitchRead(), {});
});

afterEach(() => {
	activeThreadId.set(null);
	threads.set([]);
	_resetThreadModelTruthForTests();
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

	it('keeps a listed chat on its list row and holds nothing for a thread no read has set', () => {
		applyPendingThreadModelSwitch('chat', pendingBody);
		handleSessionUpdated({ thread_id: 'elsewhere', pending_model: 'model/target', switch_status: 'preparing' });

		expect(get(threads)[0]).toMatchObject({ pending_model: 'model/target' });
		expect(get(composerSelectionFor)('chat').switching).toMatchObject({ pending_model: 'model/target' });
		expect(heldThreadSwitch('chat')).toBeUndefined();
		expect(heldThreadSwitch('elsewhere')).toBeUndefined();
		expect(get(composerSelectionFor)('elsewhere').switching).toBeUndefined();
	});
});
