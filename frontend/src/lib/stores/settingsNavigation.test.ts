import { beforeEach, describe, expect, it } from 'vitest';
import { get } from 'svelte/store';

import {
	openProjectSettings,
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
});
