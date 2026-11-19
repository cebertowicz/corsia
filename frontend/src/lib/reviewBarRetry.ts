// Three tries over about 65 s outlast a brief restart or network drop, then stop
// asking a server that is down. Return, focus and reconnect still reload the bar.
const RETRY_DELAYS = [5_000, 15_000, 45_000];

/** Schedule at most three retries after a failed review-bar read. */
export function createReviewBarRetry(retry: () => void) {
	let timer: ReturnType<typeof setTimeout> | undefined;
	let attempts = 0;
	function cancel() { clearTimeout(timer); timer = undefined; }
	function reset() { cancel(); attempts = 0; }
	function schedule() {
		cancel();
		if (attempts >= RETRY_DELAYS.length) return;
		timer = setTimeout(retry, RETRY_DELAYS[attempts++]);
	}
	return { cancel, reset, schedule };
}
