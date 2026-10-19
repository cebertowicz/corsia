import { beforeEach, describe, expect, it, vi } from 'vitest';
import { get } from 'svelte/store';

import {
	closeSettingsBack,
	listenForConnectedAppLinks,
	openProjectSettings,
	recordSettingsHomeThread,
	requestSettingsSection,
	resetSettingsNavigation,
	returnToProjectPage,
	openSectionFromProject,
	selectSettingsSection,
	settingsNavigation,
} from './settingsNavigation';

beforeEach(() => resetSettingsNavigation());

describe('settings navigation transitions', () => {
	it('requestSettingsSection clears the project page and counts the open', () => {
		openProjectSettings('p1');
		requestSettingsSection('system', 'update');
		expect(get(settingsNavigation)).toMatchObject({ section: 'system', project: null, fromProject: null, anchor: 'update', openRequest: 2 });
	});

	it('a section opened from a project returns to that project and drops a pending anchor', () => {
		openSectionFromProject('skills', { id: 'p1', name: 'One' }, 'skills-p1');
		expect(get(settingsNavigation)).toMatchObject({ section: 'skills', project: null, fromProject: { id: 'p1', name: 'One' }, anchor: 'skills-p1', openRequest: 0 });
		returnToProjectPage();
		expect(get(settingsNavigation)).toMatchObject({ section: 'projects', project: 'p1', fromProject: null, anchor: null });
	});

	it('choosing a section from the list drops a pending anchor', () => {
		requestSettingsSection('providers', 'nowhere');
		selectSettingsSection('keys');
		expect(get(settingsNavigation)).toMatchObject({ section: 'keys', anchor: null });
	});

	it('opens a connected app from the initial hash and later hash changes, then cleans up', () => {
		window.history.replaceState({}, '', '#connected-apps/google%20drive');
		const openSettings = vi.fn();
		const stop = listenForConnectedAppLinks(openSettings);
		expect(get(settingsNavigation)).toMatchObject({ section: 'connected-apps', connectedAppId: 'google drive', openRequest: 1 });

		window.history.replaceState({}, '', '#connected-apps/google-calendar');
		window.dispatchEvent(new Event('hashchange'));
		expect(get(settingsNavigation)).toMatchObject({ connectedAppId: 'google-calendar', openRequest: 2 });
		expect(openSettings).toHaveBeenCalledTimes(2);

		stop();
		window.history.replaceState({}, '', '#connected-apps/google-docs');
		window.dispatchEvent(new Event('hashchange'));
		expect(openSettings).toHaveBeenCalledTimes(2);
		window.history.replaceState({}, '', '/');
	});

	describe('the thread Settings opened over', () => {
		it('survives a Settings -> Documents -> Settings round trip', () => {
			recordSettingsHomeThread('settings', 'thread-1');
			recordSettingsHomeThread('documents', null); // Context "Add existing", Skills "Browse": the thread is gone from the route
			recordSettingsHomeThread('settings', null); // back in Settings, no thread on screen
			expect(get(settingsNavigation).homeThread).toBe('thread-1');
		});

		it.each(['chat', 'activity', 'measurement'])('is cleared when the person leaves Settings for %s', (view) => {
			recordSettingsHomeThread('settings', 'thread-1');
			recordSettingsHomeThread(view, null);
			expect(get(settingsNavigation).homeThread).toBeNull();
		});

		it('is cleared by leaving from Documents too, so a later Settings does not inherit it', () => {
			recordSettingsHomeThread('settings', 'thread-1');
			recordSettingsHomeThread('documents', null);
			recordSettingsHomeThread('chat', 'thread-2');
			recordSettingsHomeThread('settings', null);
			expect(get(settingsNavigation).homeThread).toBeNull();
		});

		it('is the only record of it: Back returns to it, once', async () => {
			recordSettingsHomeThread('settings', 'thread-1');
			const select = vi.fn(async () => undefined);
			const fallback = vi.fn();
			await closeSettingsBack(select, fallback);
			expect(select).toHaveBeenCalledWith('thread-1');
			expect(fallback).not.toHaveBeenCalled();
			expect(get(settingsNavigation).homeThread).toBeNull();
			await closeSettingsBack(select, fallback);
			expect(fallback).toHaveBeenCalledTimes(1);
		});
	});
});
