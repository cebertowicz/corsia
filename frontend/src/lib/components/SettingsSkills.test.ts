import { flushSync, mount, unmount } from 'svelte';
import { afterEach, beforeEach, describe, expect, it, vi } from 'vitest';
import SettingsSkills from './SettingsSkills.svelte';
import { token } from '$lib/stores/auth';
import { projects } from '$lib/stores/projects';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const skill = (name: string, scope: string) => ({
	name,
	description: `Use when ${name}.`,
	scope,
	scope_id: scope === 'workspace' ? 'workspace' : 'atlas',
	path: `/workspace/${scope}/skills/${name}/SKILL.md`,
	body_path: `/workspace/${scope}/skills/${name}/SKILL.md`,
	metadata: {},
	status: 'included',
	errors: [],
});

beforeEach(() => {
	host = document.createElement('div');
	document.body.appendChild(host);
	token.set('test-token');
	vi.stubGlobal('fetch', vi.fn(async (input: RequestInfo | URL) => {
		const path = String(input);
		if (path.includes('/api/skills') && path.includes('project')) {
			return { ok: true, json: async () => ({ skills: [skill('deploy', 'project')], root: '/workspace/atlas', scope_id: 'atlas' }) };
		}
		if (path.includes('/api/skills')) {
			return { ok: true, json: async () => ({ skills: [skill('review', 'workspace')], root: '/workspace', scope_id: 'workspace' }) };
		}
		if (path.startsWith('/api/projects')) {
			return { ok: true, json: async () => ({ projects: [{ id: 'atlas', name: 'Atlas', status: 'active' }] }) };
		}
		return { ok: true, json: async () => ({}) };
	}));
	projects.set([{ id: 'atlas', name: 'Atlas', status: 'active' } as never]);
});

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host.remove();
	token.set(null);
	projects.set([]);
	vi.unstubAllGlobals();
	vi.restoreAllMocks();
});

describe('SettingsSkills', () => {
	it('shows every level\'s skills on one page, all projects first, and browses for more', async () => {
		const onBrowseSkills = vi.fn();
		component = mount(SettingsSkills, { target: host, props: { onBrowseSkills } });
		flushSync();

		await vi.waitFor(() => {
			expect(host.textContent).toContain('review');
			expect(host.textContent).toContain('deploy');
		});
		const text = host.textContent ?? '';
		expect(text.indexOf('All projects')).toBeLessThan(text.indexOf('Atlas'));
		expect(host.querySelector('[data-settings-anchor="skills-atlas"]')).toBeTruthy();

		const browse = await vi.waitFor(() => {
			const button = Array.from(host.querySelectorAll<HTMLButtonElement>('button'))
				.find((candidate) => candidate.textContent?.trim() === 'Browse');
			expect(button?.disabled).toBe(false);
			return button!;
		});
		browse.click();
		expect(onBrowseSkills).toHaveBeenCalledTimes(1);
	});
});
