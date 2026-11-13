import { afterEach, describe, expect, it, vi } from 'vitest';
import { flushSync, mount, tick, unmount } from 'svelte';
import DocOwnerControl from './DocOwnerControl.svelte';
import { changeOwnership, OwnershipChangeError } from '$lib/api/docOwnership';

vi.mock('$lib/api/docOwnership', async (original) => ({ ...(await original<typeof import('$lib/api/docOwnership')>()), changeOwnership: vi.fn() }));
import type { DocOwnership } from '$lib/api/docOwnership';

const agent = (id: string, name: string, status = 'ready') => ({ id, name, status });
function ownership(patch: Partial<DocOwnership> = {}): DocOwnership {
	return {
		owner: { kind: 'agent', id: 'a1', name: 'Release planning' },
		supervisor: { id: 'a2', name: 'Research coordinator', source: 'picked', status: 'ready' },
		can_act: true, multi_person: false, can_change: true,
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
		await render(ownership({ multi_person: true }));
		item('owner').click(); await settle();
		expect(options()).not.toContain('You');
		expect(picker()!.textContent).toContain('Only an agent can own a document when more than one person uses CORSIA here.');
	});
	it('offers no choices on a document everyone shares when more than one person uses this CORSIA, and says why', async () => {
		await render(ownership({ owner: { kind: 'person', id: null, name: null }, multi_person: true, can_change: false }));
		for (const role of ['owner', 'supervisor'] as const) {
			item(role).click(); await settle();
			expect(options()).toEqual([]);
			expect(picker()!.textContent).toContain('Everyone here shares this document');
		}
		expect(changed).not.toHaveBeenCalled();
	});
	it('reads You when the person owns the document', async () => {
		await render(ownership({ owner: { kind: 'person', id: null, name: null } }));
		expect(item('owner').getAttribute('aria-label')).toBe('Owner: You');
	});
	it('reads Not set for an owner that is not set', async () => {
		await render(ownership({ owner: { kind: 'none', id: null, name: null } }));
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
	it('saves as soon as a name is picked, keeping a picked supervisor', async () => {
		const next = ownership({ owner: { kind: 'person', id: null, name: null } });
		changed.mockResolvedValue(next);
		const onChange = vi.fn();
		await render(ownership(), onChange);
		item('owner').click(); await settle();
		document.body.querySelector<HTMLElement>('[role="option"][aria-label="You"]')!.click(); await settle();
		expect(changed).toHaveBeenCalledWith('doc', { owner: 'user', supervisor: 'a2' });
		expect(onChange).toHaveBeenCalledWith(next);
		expect(picker()).toBeNull();
	});
	it('says Change failed on the item whose save failed, and what to retry in its picker', async () => {
		changed.mockRejectedValue(new Error('no'));
		await render(ownership());
		item('supervisor').click(); await settle();
		document.body.querySelector<HTMLElement>('[role="option"][aria-label="Release planning"]')!.click(); await settle();
		expect(item('supervisor').getAttribute('aria-label')).toContain('change failed');
		expect(item('supervisor').textContent).toContain('Change failed');
		expect(item('owner').textContent).not.toContain('Change failed');
		item('supervisor').click(); await settle();
		expect(picker()!.textContent).toContain('Couldn’t change the supervisor. Try again.');
	});
	it('sends no owner for a Supervisor pick', async () => {
		changed.mockResolvedValue(ownership());
		await render(ownership({ owner: { kind: 'none', id: null, name: null } }));
		item('supervisor').click(); await settle();
		document.body.querySelector<HTMLElement>('[role="option"][aria-label="Release planning"]')!.click(); await settle();
		expect(changed).toHaveBeenCalledWith('doc', { supervisor: expect.any(String) });
	});
	it('shows the server’s refusal in the picker when it sent one', async () => {
		changed.mockRejectedValue(new OwnershipChangeError(409, 'Only an agent can own a document when more than one person uses CORSIA here.'));
		await render(ownership());
		item('owner').click(); await settle();
		document.body.querySelector<HTMLElement>('[role="option"][aria-label="You"]')!.click(); await settle();
		item('owner').click(); await settle();
		expect(picker()!.textContent).toContain('Only an agent can own a document');
	});
	it('filters its list as the person types', async () => {
		await render(ownership());
		item('supervisor').click(); await settle();
		const input = document.getElementById('supervisor-choice') as HTMLInputElement;
		input.value = 'research'; input.dispatchEvent(new Event('input', { bubbles: true })); await settle();
		expect(options()).toEqual(['Research coordinator']);
	});
});
