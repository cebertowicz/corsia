import { get, writable } from 'svelte/store';

export const SETTINGS_SECTIONS = ['providers', 'system', 'keys', 'debug'] as const;
export type SettingsSection = (typeof SETTINGS_SECTIONS)[number];

export const settingsActiveSection = writable<SettingsSection>('providers');
export const settingsOpenRequest = writable(0);
/** The thread a deep link to Settings came from; Back returns there instead of to Activity. */
export const settingsReturnThread = writable<string | null>(null);
/** An element inside the opened section (its `data-settings-anchor`) to bring into view once, then cleared. */
export const settingsAnchorRequest = writable<string | null>(null);
/** The harness connection or cloud provider whose Providers card should open expanded and scroll into view. */
export const providerCardRequest = writable<string | null>(null);

export function requestSettingsSection(section: SettingsSection, anchor: string | null = null) {
	settingsActiveSection.set(section);
	settingsAnchorRequest.set(anchor);
	settingsOpenRequest.update((value) => value + 1);
}

/** Open one registered server's Providers card; a server id never collides with a provider name. */
export function requestServerCard(serverId: string) {
	requestProviderCard(serverId);
}

export function requestProviderCard(connection: string) {
	providerCardRequest.set(connection);
	requestSettingsSection('providers');
}

/** Close Settings: back to the thread a deep link came from, else to the fallback (Activity). */
export async function closeSettingsBack(selectThread: (id: string) => Promise<unknown>, fallback: () => void) {
	const id = get(settingsReturnThread);
	settingsReturnThread.set(null);
	if (id) await selectThread(id);
	else fallback();
}
