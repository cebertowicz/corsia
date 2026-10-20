import { get } from 'svelte/store';
import { afterEach, describe, expect, it } from 'vitest';
import { pendingCorsiaMessage, requestCorsiaMessage, takeCorsiaMessage } from './corsiaRequest';

afterEach(() => pendingCorsiaMessage.set(null));

describe('CORSIA message requests', () => {
	it('takes a request once and leaves a newer request alone', () => {
		const firstId = requestCorsiaMessage('Connect a Google account for all my projects');
		const first = get(pendingCorsiaMessage);
		expect(first).toEqual({ id: firstId, text: 'Connect a Google account for all my projects' });

		const secondId = requestCorsiaMessage('Connect another app');
		expect(takeCorsiaMessage(firstId)).toBeNull();
		expect(get(pendingCorsiaMessage)).toEqual({ id: secondId, text: 'Connect another app' });
		expect(takeCorsiaMessage(secondId)).toEqual({ id: secondId, text: 'Connect another app' });
		expect(get(pendingCorsiaMessage)).toBeNull();
	});
});
