import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import DocOwnerControl from './DocOwnerControl.svelte';
import { changeOwnership, OwnershipChangeError } from '$lib/api/docOwnership';

vi.mock('$lib/api/docOwnership', async (original) => ({ ...(await original<typeof import('$lib/api/docOwnership')>()), changeOwnership: vi.fn() }));
import type { DocOwnership } from '$lib/api/docOwnership';

const agent = (id: string, name: string, status = 'ready', responsibility = '') => ({ id, name, status, responsibility });
const SHARED = 'Everyone here shares this document, so its owner and supervisor can’t be changed while more than one person uses CORSIA. Ask CORSIA to give it an agent as its owner.';
const MULTI = 'Only an agent can own a document when more than one person uses CORSIA here.';
function ownership(patch: Partial<DocOwnership> = {}): DocOwnership {
	return {
		owner: { kind: 'agent', id: 'a1', name: 'Release planning', label: 'Release planning' },
		supervisor: { id: 'a2', name: 'Research coordinator', source: 'picked', status: 'ready' },
		can_act: true, multi_person: false, can_change: true, refusal: null, multi_person_refusal: null, scope: 'project',
		choices: [agent('a1', 'Release planning'), agent('a2', 'Research coordinator')],
		...patch,
	};
}
const changed = vi.mocked(changeOwnership);
let component: Record<string, unknown> | null = null;
let host: HTMLElement | null = null;
async function render(value: DocOwnership, onChange: (value: DocOwnership) => void = () => {}) {
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(DocOwnerControl, { target: host, props: { artifactId: 'doc', ownership: value, onChange } });
	flushSync(); await tick();
}
async function settle() { flushSync(); for (let i = 0; i < 4; i++) await tick(); flushSync(); }
afterEach(() => { if (component) unmount(component); component = null; host?.remove(); host = null; document.body.querySelectorAll('[data-popover]').forEach((node) => node.remove()); changed.mockReset(); });
const items = () => [...host!.querySelectorAll<HTMLButtonElement>('button.item')];
const item = (role: 'owner' | 'supervisor') => host!.querySelector<HTMLButtonElement>(`button.item[data-role="${role}"]`)!;
const picker = () => document.body.querySelector<HTMLElement>('.role-picker');
const options = () => [...document.body.querySelectorAll<HTMLElement>('[role="option"]')].map((option) => option.getAttribute('aria-label'));

describe('DocOwnerControl', () => {
	it('shows Owner and Supervisor as two separate controls', async () => {
		await render(ownership());
		expect(items().map((button) => button.getAttribute('aria-label'))).toEqual(['Owner: Release planning', 'Supervisor: Research coordinator']);
		expect(host!.querySelector('[role="group"]')!.getAttribute('aria-label')).toBe('Owner and supervisor');
	});
	it('opens a picker for the owner only, offering You on a one-person CORSIA', async () => {
		await render(ownership());
		item('owner').click(); await settle();
		expect(picker()!.dataset.role).toBe('owner');
		expect(document.getElementById('supervisor-choice')).toBeNull();
		expect(document.body.querySelector('[data-popover]')!.getAttribute('aria-label')).toBe('Owner');
		expect(options()).toEqual(['You', 'Release planning', 'Research coordinator']);
	});
	it('opens a picker for the supervisor only, without You', async () => {
		await render(ownership());
		item('supervisor').click(); await settle();
		expect(picker()!.dataset.role).toBe('supervisor');
		expect(document.getElementById('owner-choice')).toBeNull();
		expect(options()).toEqual(['Release planning', 'Research coordinator']);
	});
	it('switches from one picker to the other when the other item is pressed', async () => {
		await render(ownership());
		item('owner').click(); await settle();
		item('supervisor').click(); await settle();
		expect(document.body.querySelectorAll('.role-picker')).toHaveLength(1);
		expect(picker()!.dataset.role).toBe('supervisor');
	});
	it('does not offer You when more than one person uses this CORSIA, and says why', async () => {
		await render(ownership({ multi_person: true, multi_person_refusal: MULTI }));
		item('owner').click(); await settle();
		expect(options()).not.toContain('You');
		expect(picker()!.textContent).toContain(MULTI);
	});
	it('offers no choices on a document everyone shares when more than one person uses this CORSIA, and says why', async () => {
		await render(ownership({ owner: { kind: 'person', id: null, name: null, label: 'Everyone' }, multi_person: true, can_change: false, refusal: SHARED, choices: [] }));
		expect(item('owner').textContent).toContain('Everyone');
		for (const role of ['owner', 'supervisor'] as const) {
			item(role).click(); await settle();
			expect(options()).toEqual([]);
			expect(picker()!.textContent).toContain(SHARED);
		}
		expect(changed).not.toHaveBeenCalled();
	});
	it('reads You when the person owns the document', async () => {
		await render(ownership({ owner: { kind: 'person', id: null, name: null, label: 'You' } }));
		expect(item('owner').getAttribute('aria-label')).toBe('Owner: You');
	});
	it('reads Not set for an owner that is not set', async () => {
		await render(ownership({ owner: { kind: 'none', id: null, name: null, label: 'Not set' } }));
		expect(item('owner').textContent).toContain('Not set');
	});
	it('merges the two when one agent holds both roles, and its picker still reaches the supervisor', async () => {
		await render(ownership({ supervisor: { id: 'a1', name: 'Release planning', source: 'owner', status: 'ready' } }));
		expect(items()).toHaveLength(1);
		expect(items()[0].textContent).toContain('Owner and supervisor:');
		items()[0].click(); await settle();
		expect(picker()!.dataset.role).toBe('owner');
		picker()!.querySelector<HTMLButtonElement>('.link')!.click(); await settle();
		expect(picker()!.dataset.role).toBe('supervisor');
	});
	it('keeps a paused supervisor named, with the Paused mark', async () => {
		await render(ownership({ supervisor: { id: 'a2', name: 'Research coordinator', source: 'picked', status: 'paused' }, can_act: false }));
		expect(item('supervisor').getAttribute('aria-label')).toBe('Supervisor: Research coordinator, paused');
		expect(item('supervisor').querySelector('.mark')!.textContent).toBe('Paused');
	});
	it('saves as soon as a name is picked, sending only the owner', async () => {
		const next = ownership({ owner: { kind: 'person', id: null, name: null, label: 'You' } });
		changed.mockResolvedValue(next);
		const onChange = vi.fn();
		await render(ownership(), onChange);
		item('owner').click(); await settle();
		document.body.querySelector<HTMLElement>('[role="option"][aria-label="You"]')!.click(); await settle();
		expect(changed).toHaveBeenCalledWith('doc', { owner: 'user' });
		expect(onChange).toHaveBeenCalledWith(next);
		expect(picker()).toBeNull();
	});
	it('says Couldn’t change on the item whose save failed, and what to retry in its picker', async () => {
		changed.mockRejectedValue(new Error('no'));
		await render(ownership());
		item('supervisor').click(); await settle();
		document.body.querySelector<HTMLElement>('[role="option"][aria-label="Release planning"]')!.click(); await settle();
		expect(item('supervisor').getAttribute('aria-label')).toContain('couldn’t change');
		expect(item('supervisor').textContent).toContain('Couldn’t change');
		expect(item('owner').textContent).not.toContain('Couldn’t change');
		item('supervisor').click(); await settle();
		expect(picker()!.textContent).toContain('Couldn’t change the supervisor. Try again.');
	});
	it('sends no owner for a Supervisor pick', async () => {
		changed.mockResolvedValue(ownership());
		await render(ownership({ owner: { kind: 'none', id: null, name: null, label: 'Not set' } }));
		item('supervisor').click(); await settle();
		document.body.querySelector<HTMLElement>('[role="option"][aria-label="Release planning"]')!.click(); await settle();
		expect(changed).toHaveBeenCalledWith('doc', { supervisor: expect.any(String) });
	});
	it('shows the server’s refusal in the picker when it sent one', async () => {
		changed.mockRejectedValue(new OwnershipChangeError(409, MULTI));
		await render(ownership());
		item('owner').click(); await settle();
		document.body.querySelector<HTMLElement>('[role="option"][aria-label="You"]')!.click(); await settle();
		item('owner').click(); await settle();
		expect(picker()!.textContent).toContain('Only an agent can own a document');
	});
	it('tells agents with the same name apart by what each is responsible for', async () => {
		await render(ownership({ choices: [agent('a1', 'Planner', 'ready', 'Release planning'), agent('a2', 'Planner', 'paused', 'Research')] }));
		item('supervisor').click(); await settle();
		expect(options()).toEqual(['Planner Release planning', 'Planner Paused · Research']);
	});
	it('names the workspace, not a project, when a workspace document has no agents to choose from', async () => {
		await render(ownership({ choices: [], scope: 'workspace' }));
		item('supervisor').click(); await settle();
		expect(picker()!.textContent).toContain('There are no agents in this workspace to choose from.');
	});
	it('names the project when a project document has no agents to choose from', async () => {
		await render(ownership({ choices: [], scope: 'project' }));
		item('supervisor').click(); await settle();
		expect(picker()!.textContent).toContain('There are no agents in this project to choose from.');
	});
	it('says it couldn’t load the agents, not that there are none, when the scope couldn’t be resolved', async () => {
		await render(ownership({ choices: [], scope: null }));
		item('supervisor').click(); await settle();
		expect(picker()!.textContent).toContain('Couldn’t load the agents for this document. Reload the page to try again.');
		expect(picker()!.textContent).not.toContain('There are no agents');
		item('supervisor').click(); await settle();
		item('owner').click(); await settle();
		expect(picker()!.textContent).toContain('Couldn’t load the agents for this document. Reload the page to try again.');
	});
	it('filters its list as the person types', async () => {
		await render(ownership());
		item('supervisor').click(); await settle();
		const input = document.getElementById('supervisor-choice') as HTMLInputElement;
		input.value = 'research'; input.dispatchEvent(new Event('input', { bubbles: true })); await settle();
		expect(options()).toEqual(['Research coordinator']);
	});
});
