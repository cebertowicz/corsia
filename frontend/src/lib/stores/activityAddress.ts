import { get } from 'svelte/store';
import { activityOpenRequest, openActivity } from './activityOpen';

/**
 * An urgent notification's tap names one Activity to-do, three ways in, all read the same way:
 *  - `/?activity=<id>` when the tap opens a new window (read once on load, then removed from the address);
 *  - a `corsia-open-activity` message from the push worker when the tap lands on a window already open;
 *  - the tab's own `sessionStorage`, which holds the id from the first read until Activity opens it, so a reload during
 *    sign-in does not lose it. It is cleared as soon as Activity takes the request, so a later reload opens nothing.
 * Reading puts the request in `activityOpenRequest`, which survives sign-in and an install that is still connecting;
 * Activity opens the to-do once its list has it, the state selecting its row produces. An id Activity does not list
 * (answered, or removed since the notification was sent) opens Activity on its list with nothing selected and a line
 * saying so.
 */
export const OPEN_MESSAGE = 'corsia-open-activity';
const STORED = 'corsia_activity_address';
const ACTIVITY_PATH = /^\/\?activity=([A-Za-z0-9._~-]{1,100})$/;
const ID = /^[A-Za-z0-9._~-]{1,100}$/;

function store(): Storage | null {
	try { return window.sessionStorage; } catch { return null; }
}

let clearing = false;
/** Once Activity takes the request (it sets it to null), the stored copy has done its job. */
function clearWhenTaken(): void {
	if (clearing) return;
	clearing = true;
	let pending = get(activityOpenRequest) !== null;
	activityOpenRequest.subscribe((request) => {
		if (request !== null) { pending = true; return; }
		if (pending) store()?.removeItem(STORED);
		pending = false;
	});
}

export function openActivityFromAddress(): void {
	if (typeof window === 'undefined') return;
	const url = new URL(window.location.href);
	const fromAddress = url.searchParams.get('activity');
	if (fromAddress !== null) {
		url.searchParams.delete('activity');
		window.history.replaceState(window.history.state, '', `${url.pathname}${url.search}${url.hash}`);
	}
	const id = fromAddress ?? store()?.getItem(STORED) ?? null;
	if (id === null) return;
	if (id !== '' && !ID.test(id)) return;
	if (id !== '') store()?.setItem(STORED, id);
	clearWhenTaken();
	openActivity(id || null);
}

/** The push worker hands a window the path of the tapped item; the app opens it with the same reader as the address. */
export function listenForOpenMessages(): () => void {
	if (typeof navigator === 'undefined' || !navigator.serviceWorker) return () => undefined;
	const container = navigator.serviceWorker;
	const onMessage = (event: MessageEvent) => {
		const data = event.data as { type?: unknown; path?: unknown } | null;
		if (!data || data.type !== OPEN_MESSAGE || typeof data.path !== 'string') return;
		const id = ACTIVITY_PATH.exec(data.path)?.[1];
		if (!id) return;
		clearWhenTaken();
		openActivity(id);
	};
	container.addEventListener('message', onMessage);
	// A message queue starts on its own only for `onmessage`; this makes it start for the listener above too.
	container.startMessages?.();
	return () => container.removeEventListener('message', onMessage);
}
