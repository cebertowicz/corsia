import { describe, expect, it, vi } from 'vitest';
import { handleViewerFailure } from './docViewerFailure';

const opened = { text: null, unsaved: false, confirming: false, signal: { status: 'loading' as const } };

describe('document open failure', () => {
	it.each(['404: no such file', '404: unregistered', '404: out of scope'])('treats %s as unavailable', (message) => {
		const handlers = { signal: vi.fn(), reloadAttention: vi.fn(), fail: vi.fn() };
		handleViewerFailure(new Error(message), opened, handlers);
		expect(handlers.signal).toHaveBeenCalledWith({ status: 'unavailable' });
		expect(handlers.reloadAttention).toHaveBeenCalledOnce();
		expect(handlers.fail).not.toHaveBeenCalled();
	});
	it('keeps non-404 failures retryable', () => {
		const handlers = { signal: vi.fn(), reloadAttention: vi.fn(), fail: vi.fn() };
		handleViewerFailure(new Error('503: unavailable'), opened, handlers);
		expect(handlers.signal).toHaveBeenCalledWith({ status: 'error' });
		expect(handlers.reloadAttention).not.toHaveBeenCalled();
	});
});
