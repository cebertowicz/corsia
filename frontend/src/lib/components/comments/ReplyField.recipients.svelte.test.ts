import { afterEach, describe, expect, it } from 'vitest';
import { flushSync, mount, tick, unmount, type ComponentProps } from 'svelte';
import ReplyFieldHarness from './ReplyFieldHarness.svelte';
import { createMentionSelection } from './mentionSelection';
import type { TaggableAgent } from '$lib/api/docReviews';

let host: HTMLElement;
let component: Record<string, unknown> | null = null;
afterEach(() => { if (component) unmount(component); component = null; host?.remove(); });

const agent = (id: string, name: string): TaggableAgent => ({ id, name, responsibility: '' }) as TaggableAgent;
async function open(props: ComponentProps<typeof ReplyFieldHarness>) {
	host = document.createElement('div'); document.body.appendChild(host);
	component = mount(ReplyFieldHarness, { target: host, props });
	flushSync(); await tick(); flushSync();
}
const shown = () => Array.from(host.querySelectorAll('.rl-name'), (name) => name.textContent);

describe('the reply line names recipients by agent id', () => {
	it('shows an agent once when its name is typed in the reply and the server already named it', async () => {
		await open({ agents: [agent('a1', 'Docs lane')], goesTo: [{ id: 'a1', name: 'Docs lane' }], value: '@docs lane can you check this' });
		expect(shown()).toEqual(['Docs lane']);
	});
	it('shows two agents that share a name twice', async () => {
		const value = '@Docs lane please look';
		const selectedTags = createMentionSelection(value);
		selectedTags.select('a2', 0, '@Docs lane');
		await open({ agents: [agent('a1', 'Docs lane'), agent('a2', 'Docs lane')], goesTo: [{ id: 'a1', name: 'Docs lane' }], value, selectedTags });
		expect(shown()).toEqual(['Docs lane', 'Docs lane']);
	});
});
