import { writable } from 'svelte/store';

export const SETTINGS_SECTIONS = ['providers', 'system', 'keys', 'debug'] as const;
export type SettingsSection = (typeof SETTINGS_SECTIONS)[number];

export const settingsActiveSection = writable<SettingsSection>('providers');
export const settingsOpenRequest = writable(0);
/** An element inside the opened section (its `data-settings-anchor`) to bring into view once, then cleared. */
export const settingsAnchorRequest = writable<string | null>(null);
/** The harness connection whose Providers card should open expanded and scroll into view. */
export const providerCardRequest = writable<string | null>(null);

export function requestSettingsSection(section: SettingsSection, anchor: string | null = null) {
	settingsActiveSection.set(section);
	settingsAnchorRequest.set(anchor);
	settingsOpenRequest.update((value) => value + 1);
}

export function requestProviderCard(connection: string) {
	providerCardRequest.set(connection);
	requestSettingsSection('providers');
}
