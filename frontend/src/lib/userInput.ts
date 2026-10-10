/**
 * The page's one input clock: when this page last had the user's hand on it.
 * Pointer, key, touch or scroll anywhere, pointer movement at most once a
 * second, and the window itself gaining focus (the user coming back to the
 * tab). Focus the app moves between elements is not input: a drawer that
 * autofocuses its header on restore must not look like someone arrived.
 * Listeners go on at import, so the click that opens a surface counts, while
 * a page that was restored or reloaded with nobody at it has no input until
 * someone acts.
 *
 * Consumers apply their own window to this clock, because they ask different
 * questions: presence ends a visit after `IDLE_CAP_MS`, and the Plans pane
 * counts someone as at it within `IDLE_MS`. Neither keeps a clock of its own.
 */
export const INPUT_EVENTS = ['pointerdown', 'keydown', 'touchstart', 'wheel'] as const;
const MOVE_THROTTLE_MS = 1_000;

let lastInputAt = -Infinity;
const listeners = new Set<(at: number) => void>();

function input(): void {
	lastInputAt = Date.now();
	for (const listener of listeners) listener(lastInputAt);
}

if (typeof window !== 'undefined') {
	for (const name of INPUT_EVENTS) window.addEventListener(name, input, { passive: true, capture: true });
	// Not capturing: focus does not bubble, so only the window's own focus arrives.
	window.addEventListener('focus', (event) => {
		if (!(event.target instanceof Node)) input();
	});
	window.addEventListener('pointermove', () => {
		if (Date.now() - lastInputAt >= MOVE_THROTTLE_MS) input();
	}, { passive: true, capture: true });
}

/** The time of the last input; -Infinity before any. */
export function lastUserInputAt(): number {
	return lastInputAt;
}

/** Call `listener` on every input; returns the unsubscribe. */
export function onUserInput(listener: (at: number) => void): () => void {
	listeners.add(listener);
	return () => listeners.delete(listener);
}
