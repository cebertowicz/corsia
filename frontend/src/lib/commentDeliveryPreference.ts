import { get, writable } from 'svelte/store';
import { token } from '$lib/stores/auth';
import { connectionState } from '$lib/stores/connection';
import { on } from '$lib/ws/events';

const LEGACY_KEY = 'corsia.document-comments.deliver-immediately';
const ROUTE = '/api/doc-feedback/delivery-preference';

/** The person's "Deliver immediately" choice. The server keeps it, so it follows the person to every browser and device. */
export const deliverCommentsImmediately = writable(legacyValue());

/** What this browser kept before the server did, so the toggle reads right until the server answers. */
function legacyValue(): boolean {
	if (typeof localStorage === 'undefined') return true;
	return localStorage.getItem(LEGACY_KEY) !== 'false';
}

function forgetLegacy(): void {
	if (typeof localStorage !== 'undefined') localStorage.removeItem(LEGACY_KEY);
}

async function call(method: 'GET' | 'PUT', immediate?: boolean): Promise<{ immediate: boolean; stored: boolean } | null> {
	try {
		const res = await fetch(ROUTE, {
			method,
			headers: { Authorization: `Bearer ${get(token) ?? ''}`, ...(method === 'PUT' ? { 'Content-Type': 'application/json' } : {}) },
			...(method === 'PUT' ? { body: JSON.stringify({ immediate }) } : {}),
		});
		return res.ok ? await res.json() : null;
	} catch {
		return null;
	}
}

let reading: Promise<void> | null = null;

/**
 * Reads the person's choice from the server. A browser that chose "off" before the server kept it hands that choice over
 * once, when the server has none yet, so an existing choice survives the upgrade.
 */
export function refreshDeliveryPreference(): Promise<void> {
	if (!get(token)) return Promise.resolve();
	reading ??= (async () => {
		const server = await call('GET');
		if (!server) return;
		if (!server.stored && legacyValue() === false) {
			const carried = await call('PUT', false);
			if (!carried) return;
			deliverCommentsImmediately.set(false);
		} else {
			deliverCommentsImmediately.set(server.immediate);
		}
		forgetLegacy();
	})().finally(() => { reading = null; });
	return reading;
}

/** Saves the person's choice; resolves false, and puts the toggle back, when the server did not keep it. */
export async function setDeliverCommentsImmediately(value: boolean): Promise<boolean> {
	const before = get(deliverCommentsImmediately);
	deliverCommentsImmediately.set(value);
	const saved = await call('PUT', value);
	if (saved) return true;
	deliverCommentsImmediately.set(before);
	return false;
}

/**
 * Keeps the toggle in step with the person's other clients: a change made elsewhere arrives as a frame, and every connect
 * re-reads what a dropped connection missed. Returns one stop.
 */
export function startDeliveryPreferenceSync(): () => void {
	const stopFrames = on('doc_delivery_preference_changed', (data: { immediate?: unknown }) => {
		if (typeof data.immediate === 'boolean') deliverCommentsImmediately.set(data.immediate);
	});
	const stopConnect = connectionState.subscribe((state) => {
		if (state === 'connected') void refreshDeliveryPreference();
	});
	return () => { stopConnect(); stopFrames(); };
}

/** The lane a person's comment asks for: the same value the toggle shows. */
export function documentCommentDeliveryPolicy(): 'immediate' | 'document-held' {
	return get(deliverCommentsImmediately) ? 'immediate' : 'document-held';
}
