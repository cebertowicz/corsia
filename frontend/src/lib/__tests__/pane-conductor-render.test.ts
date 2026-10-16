import { describe, it, expect } from 'vitest';
import { mount, unmount } from 'svelte';
import Pane from '$lib/components/Pane.svelte';
import { buildPaneConfig } from '$lib/homePaneConfig';
import type { Thread } from '$lib/stores/threads';

const SCOPE = { id: 'project-1', name: 'Project' };

function thread(id: string): Thread {
	return {
		id,
		user_id: 'user-1',
		profile: 'personal',
		title: id,
		model: null,
		project_id: SCOPE.id,
		created_at: new Date().toISOString(),
		updated_at: new Date().toISOString(),
	};
}

function chatSections(id: string) {
	const config = buildPaneConfig('chat', thread(id), SCOPE);
	if (!config) throw new Error('expected chat pane');
	return config.sections;
}

describe('Pane role parity', () => {
	it('renders identical right-pane sections for conductor and ordinary threads', () => {
		const conductorSections = chatSections('conductor-thread');
		const ordinarySections = chatSections('ordinary-thread');
		expect(conductorSections).toEqual(ordinarySections);

		const conductorHost = document.createElement('div');
		const ordinaryHost = document.createElement('div');
		document.body.append(conductorHost, ordinaryHost);
		const conductorPane = mount(Pane, { target: conductorHost, props: { sections: conductorSections } });
		const ordinaryPane = mount(Pane, { target: ordinaryHost, props: { sections: ordinarySections } });

		const labels = (host: HTMLElement) => [...host.querySelectorAll('.cap')].map((cap) => cap.textContent?.trim());
		expect(labels(conductorHost)).toEqual(['Plan', 'Artifacts']);
		expect(labels(conductorHost)).toEqual(labels(ordinaryHost));

		unmount(conductorPane);
		unmount(ordinaryPane);
		conductorHost.remove();
		ordinaryHost.remove();
	});
});
