import type { LiveDocumentSession } from './liveDocumentSession';
import type { LiveOperationError } from './documentFailures';

/** Own the timer and listener for each tab's transient history-lock refusal. */
export function createBusyRetries(options: {
	run: (key: string) => void;
	show: (key: string, live: LiveDocumentSession, error: LiveOperationError) => void;
	failed: (key: string, error: LiveOperationError, visible: boolean) => void;
}) {
	const retries = new Map<string, { failures: number; visible: boolean; live: LiveDocumentSession; error: LiveOperationError; timer?: ReturnType<typeof setTimeout>; stop: () => void }>();
	function clear(key: string): void {
		const retry = retries.get(key);
		if (!retry) return;
		if (retry.timer) clearTimeout(retry.timer);
		retry.stop();
		retries.delete(key);
	}
	function reveal(key: string): void {
		const retry = retries.get(key);
		if (!retry) return;
		if (!retry.visible) options.show(key, retry.live, retry.error);
		retry.visible = true;
		options.failed(key, retry.error, true);
	}
	function schedule(key: string, live: LiveDocumentSession, error: LiveOperationError): void {
		let retry = retries.get(key);
		if (!retry) {
			const stop = live.onFrame(frame => {
				if (frame.type === 'doc_saved' && frame.server_saved !== false && live.durability.saved) options.run(key);
			});
			retry = { failures: 0, visible: false, live, error, stop };
			retries.set(key, retry);
		}
		retry.failures++;
		retry.error = error;
		if (retry.failures >= 2 || retry.visible) reveal(key);
		else options.failed(key, error, false);
		if (retry.timer) clearTimeout(retry.timer);
		const delay = Math.min(1000 * 2 ** (retry.failures - 1), 8000);
		retry.timer = setTimeout(() => { retry!.timer = undefined; options.run(key); }, delay);
	}
	return { clear, schedule, reveal, has: (key: string) => retries.has(key) };
}
