import { describe, expect, it } from 'vitest';

import { heldSignedOutInput, heldSignedOutNotice } from './heldAccountNotice';
import type { TranscriptMessage } from './messageListModel';
import type { ThreadVendorAccount } from '$lib/stores/threadVendorAccounts';

const held = (id: string, extra: Record<string, unknown> = {}) => ({
	id, thread_id: 'chat', role: 'user', content: id, delivery_state: 'queued', write_outcome: 'proved_no_write',
	delivery_error: 'account_signed_out', ...extra,
}) as unknown as TranscriptMessage;

describe('held signed-out input', () => {
	it('is the oldest queued input of this chat held for a signed-out account', () => {
		const messages = [held('other', { thread_id: 'elsewhere' }), held('limit', { delivery_error: 'account_limited' }), held('first'), held('second')];
		expect(heldSignedOutInput(messages, 'chat')?.id).toBe('first');
		expect(heldSignedOutInput(messages, null)).toBeUndefined();
	});

	it('gives the same line and cause an expired login writes, naming the held account', () => {
		const accounts = [{ id: 'free', provider: 'claude-code', name: 'Free', hold_reason: null }, { id: 'studio', provider: 'claude-code', name: 'Studio', hold_reason: 'account_signed_out' }] as unknown as ThreadVendorAccount[];
		const notice = heldSignedOutNotice(held('first'), accounts)!;
		expect(notice.role).toBe('system');
		// backend/subagents/cc_runtime_errors.py writes this line for an expired Claude Code login.
		expect(notice.content).toBe('Studio is signed out of Claude Code. Sign it in again under Settings › Providers.');
		expect(notice.metadata).toMatchObject({ cause_kind: 'sign_in_expired', account_id: 'studio' });
	});

	it('writes no notice until an account of the chat reads as held for sign-in', () => {
		expect(heldSignedOutNotice(held('first'), [])).toBeNull();
	});
});
