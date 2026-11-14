import { describe, expect, it } from 'vitest';
import { openLinkTab, settleLinkTab } from './artifactLinkTabs';
import { emptyWorkspace, openTab } from './artifactTabs';

const LINK = { href: 'shared/linked.md', projectId: 'project-1' };
let counter = 0;
const nextId = () => `link-${++counter}`;
const doc = (path: string, artifactId: string | null = null) => ({ artifactId, path, title: path.split('/').at(-1)! });

describe('a pressed link tab', () => {
	it('opens at once in its loading state and is the active tab', () => {
		const { ws, instanceId, started } = openLinkTab(emptyWorkspace(), LINK, 'linked.md', nextId);
		expect(started).toBe(true);
		expect(ws.activeTabId).toBe(instanceId);
		expect(ws.visible).toBe(true);
		expect(ws.activeSurface).toBe('artifact');
		expect(ws.tabs).toEqual([expect.objectContaining({ kind: 'document', status: 'loading', link: LINK, path: '', artifactId: null })]);
	});

	it('a second press while loading selects the same tab and starts nothing', () => {
		const first = openLinkTab(openTab(emptyWorkspace(), doc('/w/other.md')), LINK, 'linked.md', nextId);
		const second = openLinkTab({ ...first.ws, activeTabId: first.ws.tabs[0].instanceId }, LINK, 'linked.md', nextId);
		expect(second.started).toBe(false);
		expect(second.instanceId).toBe(first.instanceId);
		expect(second.ws.tabs).toHaveLength(2);
		expect(second.ws.activeTabId).toBe(first.instanceId);
	});

	it('a press on a tab the link failed in loads that tab again, without a duplicate', () => {
		const first = openLinkTab(emptyWorkspace(), LINK, 'linked.md', nextId);
		const failed = { ...first.ws, tabs: first.ws.tabs.map((tab) => ({ ...tab, status: 'error' as const })) };
		const again = openLinkTab(failed, LINK, 'linked.md', nextId);
		expect(again.started).toBe(true);
		expect(again.instanceId).toBe(first.instanceId);
		expect(again.ws.tabs).toEqual([expect.objectContaining({ status: 'loading' })]);
	});

	it('the same href from another scope is another link', () => {
		const first = openLinkTab(emptyWorkspace(), LINK, 'linked.md', nextId);
		const other = openLinkTab(first.ws, { ...LINK, projectId: null }, 'linked.md', nextId);
		expect(other.started).toBe(true);
		expect(other.ws.tabs).toHaveLength(2);
	});

	it('settles into the document tab in the same place, keeping its instance', () => {
		let ws = openTab(emptyWorkspace(), doc('/w/a.md'));
		const pressed = openLinkTab(ws, LINK, 'linked.md', nextId);
		ws = openTab(pressed.ws, doc('/w/b.md'));
		const settled = settleLinkTab({ ...ws, activeTabId: pressed.instanceId }, pressed.instanceId, doc('/w/shared/linked.md', 'art-1'))!;
		expect(settled.tabs.map((tab) => tab.path)).toEqual(['/w/a.md', '/w/shared/linked.md', '/w/b.md']);
		expect(settled.tabs[1]).toEqual(expect.objectContaining({ instanceId: pressed.instanceId, artifactId: 'art-1' }));
		expect(settled.tabs[1].link).toBeUndefined();
		expect(settled.activeTabId).toBe(pressed.instanceId);
	});

	it('gives way to the tab the document already has', () => {
		const ws = openTab(emptyWorkspace(), doc('/w/shared/linked.md', 'art-1'));
		const pressed = openLinkTab(ws, LINK, 'linked.md', nextId);
		const settled = settleLinkTab(pressed.ws, pressed.instanceId, doc('/w/shared/linked.md', 'art-1'))!;
		expect(settled.tabs).toHaveLength(1);
		expect(settled.activeTabId).toBe(ws.tabs[0].instanceId);
	});

	it('opens nothing once the person closed the loading tab', () => {
		const pressed = openLinkTab(emptyWorkspace(), LINK, 'linked.md', nextId);
		expect(settleLinkTab(emptyWorkspace(), pressed.instanceId, doc('/w/shared/linked.md'))).toBeNull();
	});

	it('is never taken for a document tab when another document opens', () => {
		const pressed = openLinkTab(emptyWorkspace(), LINK, 'linked.md', nextId);
		const ws = openTab(pressed.ws, { artifactId: null, path: '', title: 'x' });
		expect(ws.tabs).toHaveLength(2);
	});
});
