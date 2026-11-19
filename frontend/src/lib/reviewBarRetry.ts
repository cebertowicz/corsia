// This timer covers a read that failed or hung while the socket stays up. It is not a restart
// recovery: after a restart the socket reconnects, and the reconnect trigger already reloads the bar.
// Decision: after three tries (about 65 s) the existing triggers (return, focus, reconnect) take over,
// so the bar never polls forever against a server that is down.
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
