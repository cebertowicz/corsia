import { afterEach, expect, it, vi } from 'vitest';
import { createReviewBarRetry } from './reviewBarRetry';

afterEach(() => vi.useRealTimers());

it('keeps only one pending retry when scheduled twice', async () => {
	vi.useFakeTimers();
	const read = vi.fn();
	const retry = createReviewBarRetry(read);
	retry.schedule();
	retry.schedule();
	await vi.advanceTimersByTimeAsync(65_000);
	expect(read).toHaveBeenCalledTimes(1);
	retry.cancel();
});
