import { get, writable } from 'svelte/store';

export const SETTINGS_SECTIONS = ['providers', 'skills', 'context', 'projects', 'keys', 'system', 'debug'] as const;
export type SettingsSection = (typeof SETTINGS_SECTIONS)[number];

export const settingsActiveSection = writable<SettingsSection>('providers');
/** The project whose page is open under Projects; null shows the project list. */
export const settingsProject = writable<string | null>(null);
/** The project page a "What this project can use" row came from; Back returns to it. */
export const settingsFromProject = writable<{ id: string; name: string } | null>(null);

export interface SettingsDrill {
	title: string;
	backLabel?: string;
	onBack: () => void;
}
/** The app header while Settings is drilled into a page: it names the page and goes up one level. */
export const settingsDrill = writable<SettingsDrill | null>(null);
export const settingsOpenRequest = writable(0);
/** The thread a deep link to Settings came from; Back returns there instead of to Activity. */
export const settingsReturnThread = writable<string | null>(null);
/** The thread on screen when Settings opened: a file opened from Settings becomes its tab. Null outside Settings. */
export const settingsHomeThread = writable<string | null>(null);
/** An element inside the opened section (its `data-settings-anchor`) to bring into view once, then cleared. */
export const settingsAnchorRequest = writable<string | null>(null);
/** The harness connection or cloud provider whose Providers card should open expanded and scroll into view. */
export const providerCardRequest = writable<string | null>(null);

export function requestSettingsSection(section: SettingsSection, anchor: string | null = null) {
	if (section !== 'projects') settingsProject.set(null);
	settingsFromProject.set(null);
	settingsActiveSection.set(section);
	settingsAnchorRequest.set(anchor);
	settingsOpenRequest.update((value) => value + 1);
}

/** The workspace gear: Settings at its first section. */
export function openWorkspaceSettings(anchor: string | null = null) {
	requestSettingsSection('providers', anchor);
}

/** A project's gear: Settings at Projects, on that project's page. */
export function openProjectSettings(projectId: string, anchor: string | null = null) {
	settingsProject.set(projectId);
	requestSettingsSection('projects', anchor);
}

/** Inside Settings: a project page's "What this project can use" row opens that section at the project's level. */
export function openSectionFromProject(section: SettingsSection, project: { id: string; name: string }, anchor: string | null) {
	settingsProject.set(null);
	settingsFromProject.set(project);
	settingsActiveSection.set(section);
	settingsAnchorRequest.set(anchor);
}

/** Back from a section opened by a project page: that project's page again. */
export function returnToProjectPage() {
	const from = get(settingsFromProject);
	settingsFromProject.set(null);
	settingsActiveSection.set('projects');
	settingsProject.set(from?.id ?? null);
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
