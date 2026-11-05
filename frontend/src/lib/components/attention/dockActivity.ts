/** Re-judge auto-open after typing settles or focus moves. */
export function watchDockActivity(idleMs: number, onActivity: () => void, onRejudge: () => void): () => void {
	let idleTimer: ReturnType<typeof setTimeout> | undefined;
	const noteActivity = () => {
		onActivity();
		clearTimeout(idleTimer);
		idleTimer = setTimeout(onRejudge, idleMs);
	};
	const rejudge = () => { setTimeout(onRejudge, 0); };
	document.addEventListener('keydown', noteActivity, true);
	document.addEventListener('input', noteActivity, true);
	document.addEventListener('focusin', rejudge);
	document.addEventListener('focusout', rejudge);
	return () => {
		clearTimeout(idleTimer);
		document.removeEventListener('keydown', noteActivity, true);
		document.removeEventListener('input', noteActivity, true);
		document.removeEventListener('focusin', rejudge);
		document.removeEventListener('focusout', rejudge);
	};
}
