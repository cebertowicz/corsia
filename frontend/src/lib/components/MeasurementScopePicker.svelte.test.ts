import { afterEach, describe, expect, it } from 'vitest';
import { flushSync, mount, unmount } from 'svelte';
import MeasurementScopePicker from './MeasurementScopePicker.svelte';
import source from './MeasurementScopePicker.svelte?raw';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;

const projects = [
	{ id: 'prj-atlas', name: 'Atlas' },
	{ id: 'prj-boreas', name: 'Boreas' },
];
const agents = [
	{ id: 'ag-a', name: 'Bugs', projectId: 'prj-atlas', projectName: 'Atlas' },
	{ id: 'ag-b', name: 'Bugs', projectId: 'prj-boreas', projectName: 'Boreas' },
	{ id: 'ag-c', name: 'Context & Memory', projectId: 'prj-atlas', projectName: 'Atlas' },
];

afterEach(() => {
	if (component) unmount(component);
	component = null;
	host?.remove();
});

function render(picked: { project?: string | 'all'; agent?: string | null } = {}): { chosen: string[] } {
	host = document.createElement('div');
	document.body.appendChild(host);
	const chosen: string[] = [];
	component = mount(MeasurementScopePicker, {
		target: host,
		props: {
			projects,
			agents,
			project: picked.project ?? 'all',
			agent: picked.agent ?? null,
			onSelectProject: (id: string) => chosen.push(`project:${id}`),
			onSelectAgent: (row: { id: string }) => chosen.push(`agent:${row.id}`),
		},
	});
	flushSync();
	return { chosen };
}

function openPicker(): void {
	(host.querySelector('.picker-trigger') as HTMLButtonElement).click();
	flushSync();
}

function search(term: string): void {
	const input = panel()?.querySelector('.picker-search') as HTMLInputElement;
	input.value = term;
	input.dispatchEvent(new Event('input', { bubbles: true }));
	flushSync();
}

// The shared popover is portaled to document.body to escape the page pane's clip boundary.
const panel = () => document.querySelector('.popover');
const optionText = () => [...document.querySelectorAll('.picker-option')].map((node) => node.textContent?.replace(/\s+/g, ' ').trim());

describe('MeasurementScopePicker', () => {
	it('names the current scope on the trigger and opens a searchable listbox', () => {
		render();
		const trigger = host.querySelector('.picker-trigger') as HTMLButtonElement;
		expect(trigger.getAttribute('aria-label')).toBe('Showing: All work');
		expect(trigger.getAttribute('aria-expanded')).toBe('false');
		openPicker();
		expect(panel()?.querySelector('[role="listbox"]')).not.toBeNull();
		expect((panel()?.querySelector('.picker-search') as HTMLInputElement).getAttribute('aria-label')).toBe('Search projects and agents');
	});

	it('filters projects and agents by the search term', () => {
		render();
		openPicker();
		expect(optionText()).toContain('All work');
		search('boreas');
		expect(optionText().some((text) => text?.startsWith('Boreas'))).toBe(true);
		expect(optionText().some((text) => text?.startsWith('Atlas'))).toBe(false);
		search('context');
		// The matching agent remains visibly grouped under its project.
		expect(optionText()).toEqual(['Atlas All agents in this project', 'Context & Memory Atlas']);
	});

	it('keeps the project name on every same-named agent result so they stay distinguishable', () => {
		render();
		openPicker();
		search('bugs');
		const rows = [...document.querySelectorAll('.picker-option.is-nested')];
		expect(rows.length).toBe(2);
		expect(rows.map((row) => row.querySelector('.option-label')?.textContent)).toEqual(['Bugs', 'Bugs']);
		expect(rows.map((row) => row.querySelector('.option-detail')?.textContent)).toEqual(['Atlas', 'Boreas']);
	});

	it('reports the chosen project on the trigger and returns the picked agent', () => {
		const { chosen } = render({ agent: 'ag-b' });
		expect((host.querySelector('.picker-trigger') as HTMLButtonElement).getAttribute('aria-label')).toBe('Showing: Boreas');
		openPicker();
		search('bugs');
		const second = [...document.querySelectorAll('.picker-option')].find((row) => row.querySelector('.option-detail')?.textContent === 'Boreas') as HTMLButtonElement;
		second.click();
		flushSync();
		expect(chosen).toEqual(['agent:ag-b']);
		expect(panel()).toBeNull();
	});

	it('is keyboard-operable: arrows update the active descendant, options stay out of Tab order, and Enter chooses it', () => {
		const { chosen } = render();
		openPicker();
		const input = panel()?.querySelector('.picker-search') as HTMLInputElement;
		expect(input.getAttribute('aria-activedescendant')).toBe('scope-all');
		expect([...document.querySelectorAll('[role="option"]')].every((option) => option.getAttribute('tabindex') === '-1')).toBe(true);
		input.dispatchEvent(new KeyboardEvent('keydown', { key: 'ArrowDown', bubbles: true }));
		flushSync();
		expect(input.getAttribute('aria-activedescendant')).toBe('scope-project-prj-atlas');
		expect(document.querySelector('[data-active="true"] .option-label')?.textContent).toBe('Atlas');
		input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Enter', bubbles: true }));
		flushSync();
		expect(chosen).toEqual(['project:prj-atlas']);
		expect(panel()).toBeNull();
	});

	it('closes on Escape without choosing', () => {
		const { chosen } = render();
		openPicker();
		const input = panel()?.querySelector('.picker-search') as HTMLInputElement;
		input.dispatchEvent(new KeyboardEvent('keydown', { key: 'Escape', bubbles: true }));
		flushSync();
		expect(panel()).toBeNull();
		expect(chosen).toEqual([]);
	});

	// The panel and its highlighted rows are painted by the app shell, so the only colour this sheet
	// chooses against here is its own; these declarations are what the contrast ratio is measured on.
	it('keeps the detail line readable on the panel and on a highlighted row', () => {
		const css = source.slice(source.indexOf('<style>'));
		expect(css).toContain('.option-detail { color: var(--text-secondary);');
		expect(css).not.toMatch(/color:\s*var\(--text-tertiary\)/);
		expect(css).toContain('.picker-option:hover .option-detail, .picker-option.active .option-detail { color: inherit; }');
		expect(css).toContain('.picker-option:hover, .picker-option.active { color: var(--text-primary); }');
		expect(css).toContain('.picker-search::placeholder { color: var(--text-secondary); opacity: 1; }');
	});
});
