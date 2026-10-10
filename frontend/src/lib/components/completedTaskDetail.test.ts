import { describe, expect, it } from 'vitest';
import type { OperatorNode } from '$lib/api/plans';
import { breadcrumbName, indexNodes, taskDetail } from './completedTaskDetail';

const node = (id: string, parent: string | null, over: Partial<OperatorNode> = {}) => ({
	id, parent_id: parent, title: `Title ${id}`, description: null, acceptance_criteria: '', ...over,
}) as OperatorNode;

describe('completed task detail', () => {
	it('walks parent_id to a top-first breadcrumb without the task itself', () => {
		const byId = indexNodes([node('a', null), node('b', 'a'), node('c', 'b'), node('d', 'c')]);
		const detail = taskDetail(byId, 'd')!;
		expect(detail.crumbs).toEqual(['Title a', 'Title b', 'Title c']);
		expect(detail.gap).toBe(false);
		expect(breadcrumbName(detail)).toBe('Title a › Title b › Title c');
	});

	it('has no breadcrumb for a top-level task', () => {
		expect(taskDetail(indexNodes([node('a', null)]), 'a')).toMatchObject({ crumbs: [], gap: false });
	});

	it('prefers the description and falls back to the acceptance criteria', () => {
		const byId = indexNodes([
			node('a', null, { description: ' Why it matters ', acceptance_criteria: 'Ignored' }),
			node('b', null, { description: '  ', acceptance_criteria: 'Tests pass' }),
			node('c', null),
		]);
		expect(taskDetail(byId, 'a')!.text).toBe('Why it matters');
		expect(taskDetail(byId, 'b')!.text).toBe('Done when: Tests pass');
		expect(taskDetail(byId, 'c')!.text).toBeNull();
	});

	it('marks a path that stops at a missing ancestor', () => {
		const detail = taskDetail(indexNodes([node('b', 'gone'), node('c', 'b')]), 'c')!;
		expect(detail.crumbs).toEqual(['Title b']);
		expect(detail.gap).toBe(true);
		expect(breadcrumbName(detail)).toBe('… › Title b');
	});

	it('is null for a task the read does not hold and survives a parent cycle', () => {
		expect(taskDetail(indexNodes([node('a', null)]), 'x')).toBeNull();
		const cyclic = taskDetail(indexNodes([node('a', 'b'), node('b', 'a')]), 'a')!;
		expect(cyclic.crumbs).toEqual(['Title b']);
		expect(cyclic.gap).toBe(false);
	});
});
