import { describe, expect, it } from 'vitest';
import {
	toolDetailResult,
	toolDisplayArgs,
	toolKind,
	toolOutcomeLabel,
	summarizeTools,
	diffPayload,
	skillLabel,
} from './toolMeta';

describe('toolKind', () => {
	it('maps CORSIA and CC names onto shared kinds', () => {
		expect(toolKind('read_file')).toBe('read');
		expect(toolKind('Read')).toBe('read');
		expect(toolKind('edit_file')).toBe('edit');
		expect(toolKind('Edit')).toBe('edit');
		expect(toolKind('multi_edit')).toBe('edit');
		expect(toolKind('run_command')).toBe('exec');
		expect(toolKind('Bash')).toBe('exec');
		expect(toolKind('delegate_task')).toBe('agent');
		expect(toolKind('project_create')).toBe('write');
		expect(toolKind('doc_comment_create')).toBe('write');
		expect(toolKind('doc_feedback_send')).toBe('write');
		expect(toolKind('artifact_resolve')).toBe('read');
		expect(toolKind('write_knowledge')).toBe('write');
		expect(toolKind('Task')).toBe('agent');
		expect(toolKind('whatever_else')).toBe('generic');
	});

	it('classifies review requests as document writes across each transport', () => {
		expect(toolKind('doc_review_request')).toBe('write');
		expect(toolKind('mcp__corsia__doc_review_request')).toBe('write');
		expect(toolKind('mcp__corsia__corsia_tool', { name: 'doc_review_request', arguments: {} })).toBe('write');
	});

	it('maps the CORSIA MCP bridge wrapper to the inner CORSIA tool', () => {
		const args = JSON.stringify({
			name: 'run_command',
			arguments: { command: 'git status --short' },
		});
		expect(toolKind('mcp__corsia__corsia_tool', args)).toBe('exec');
	});

	it('maps direct CORSIA MCP tool names without leaking the transport prefix', () => {
		expect(toolKind('mcp__corsia__read_file', { path: '/tmp/a.md' })).toBe('read');
		expect(toolKind('mcp__corsia__delegate_task', { task: 'check this' })).toBe('agent');
	});

	it('normalizes lowercase direct MCP Bash, Grep, and Glob names', () => {
		expect(toolKind('mcp__corsia__bash', { command: 'git status' })).toBe('exec');
		expect(toolKind('mcp__corsia__grep', { pattern: 'ToolActivityBlock' })).toBe('search');
		expect(toolKind('mcp__corsia__glob', { pattern: '*.svelte' })).toBe('search');
	});
});

describe('toolOutcomeLabel', () => {
	it('reads done and refused as projected', () => {
		expect(toolOutcomeLabel('done')).toBe('done');
		expect(toolOutcomeLabel('refused')).toBe('refused');
	});

	it('reads a synthetic unknown outcome as unknown, never failed', () => {
		expect(toolOutcomeLabel('unknown', 'No result was recorded')).toBe('Outcome unknown');
	});

	it('claims nothing for a running or unsettled call', () => {
		expect(toolOutcomeLabel(undefined)).toBe('');
		expect(toolOutcomeLabel(null, null)).toBe('');
	});

	it('names a failure with the backend-projected error line', () => {
		expect(toolOutcomeLabel('failed', 'Error: permission denied')).toBe(
			'failed — Error: permission denied'
		);
		expect(toolOutcomeLabel('failed', null)).toBe('failed');
	});
});

describe('summarizeTools', () => {
	it('groups by kind in first-occurrence order with counts', () => {
		const segs = summarizeTools([
			{ name: 'Read', object: '/a.py' },
			{ name: 'Read', object: '/b.py' },
			{ name: 'Edit', object: '/a.py' },
			{ name: 'Bash', object: 'ls' },
			{ name: 'Bash', object: 'pwd' },
			{ name: 'Read', object: '/c.py' },
		]);
		expect(segs.map((s) => s.label)).toEqual([
			'Read 3 files', 'Edited a.py', 'Ran 2 commands',
		]);
	});

	it('dedupes repeat objects per file kind', () => {
		const segs = summarizeTools([
			{ name: 'Read', object: '/same.py' },
			{ name: 'Read', object: '/same.py' },
		]);
		expect(segs[0].label).toBe('Read same.py');
	});

	it('agent runs summarize like CC desktop', () => {
		const segs = summarizeTools([
			{ name: 'Task', args: '{}' }, { name: 'Task', args: '{}' },
		]);
		expect(segs[0].label).toBe('Ran 2 agents');
	});

	it('summarizes CORSIA MCP bridge calls by inner tool kind', () => {
		const segs = summarizeTools([
			{
				name: 'mcp__corsia__corsia_tool',
				args: JSON.stringify({
					name: 'read_file',
					arguments: { path: '/tmp/notes.md' },
				}),
				object: '/tmp/notes.md',
			},
			{
				name: 'mcp__corsia__corsia_tool',
				args: JSON.stringify({
					name: 'run_command',
					arguments: { command: 'pwd' },
				}),
				object: 'pwd',
			},
		]);
		expect(segs.map((s) => s.label)).toEqual(['Read notes.md', 'Ran pwd']);
	});

	it('summarizes direct CORSIA MCP calls by underlying tool kind', () => {
		const segs = summarizeTools([
			{ name: 'mcp__corsia__read_file', object: '/tmp/notes.md' },
			{ name: 'mcp__corsia__run_command', object: 'pwd' },
		]);
		expect(segs.map((s) => s.label)).toEqual(['Read notes.md', 'Ran pwd']);
	});

	it('uses the projected object for one-tool Bash, Grep, and Glob summaries', () => {
		expect(summarizeTools([{
			name: 'mcp__corsia__bash', object: 'Check the worktree',
		}])[0].label).toBe('Ran Check the worktree');
		expect(summarizeTools([{
			name: 'mcp__corsia__grep', object: 'ToolActivityBlock',
		}])[0].label).toBe('Searched ToolActivityBlock');
		expect(summarizeTools([{
			name: 'mcp__corsia__glob', object: '*.svelte',
		}])[0].label).toBe('Searched *.svelte');
	});

	it('names nothing when the backend projected no object', () => {
		expect(summarizeTools([{ name: 'bash', args: '{"command": "ls"}' }])[0].label).toBe('Ran a command');
	});
});

describe('diffPayload', () => {
	it('extracts CC Edit args', () => {
		const p = diffPayload('Edit', JSON.stringify({
			file_path: '/a.py', old_string: 'x = 1', new_string: 'x = 2',
		}));
		expect(p).toEqual({ file: '/a.py', oldText: 'x = 1', newText: 'x = 2' });
	});

	it('extracts CORSIA edit_file args', () => {
		const p = diffPayload('edit_file', JSON.stringify({
			path: 'b.ts', old_string: 'a', new_string: 'b',
		}));
		expect(p).toEqual({ file: 'b.ts', oldText: 'a', newText: 'b' });
	});

	it('still renders diffs for persisted legacy edit args', () => {
		const p = diffPayload('edit_file', JSON.stringify({
			path: 'old.ts', old_text: 'before', new_text: 'after',
		}));
		expect(p).toEqual({ file: 'old.ts', oldText: 'before', newText: 'after' });
	});

	it('write tools diff against empty', () => {
		const p = diffPayload('Write', JSON.stringify({ file_path: '/n.md', content: 'hi' }));
		expect(p).toEqual({ file: '/n.md', oldText: '', newText: 'hi' });
	});

	it('null for non-edit tools and malformed args', () => {
		expect(diffPayload('Bash', '{"command": "ls"}')).toBeNull();
		expect(diffPayload('Edit', 'truncated{')).toBeNull();
	});

	it('extracts diffs from CORSIA MCP bridge edit calls', () => {
		const p = diffPayload('mcp__corsia__corsia_tool', JSON.stringify({
			name: 'edit_file',
			arguments: { path: 'b.ts', old_string: 'a', new_string: 'b' },
		}));
		expect(p).toEqual({ file: 'b.ts', oldText: 'a', newText: 'b' });
	});
});

describe('toolDisplayArgs and toolDetailResult', () => {
	it('unwrap CORSIA MCP bridge arguments and result wrappers', () => {
		const args = JSON.stringify({
			name: 'run_command',
			arguments: { command: 'pwd' },
		});
		expect(toolDisplayArgs('mcp__corsia__corsia_tool', args)).toEqual({
			command: 'pwd',
		});
		expect(toolDetailResult('mcp__corsia__corsia_tool', '{"result":"ok"}')).toBe('ok');
	});
});

describe('skill labels', () => {
	const TASK_SKILL = '/w/.corsia/skills/task-discipline/SKILL.md';
	const GITHUB_SKILL = '/w/.corsia/skills/github/SKILL.md';

	it('names the skill a single read consulted', () => {
		expect(summarizeTools([{ name: 'read_file', object: TASK_SKILL }])[0].label).toBe(
			'Read task-discipline skill'
		);
	});

	it('lists both names when a turn reads two skills', () => {
		const segs = summarizeTools([
			{ name: 'read_file', object: TASK_SKILL },
			{ name: 'Read', object: GITHUB_SKILL },
		]);
		expect(segs.map((s) => s.label)).toEqual(['Read 2 skills: task-discipline, github']);
	});

	it('names the skill a reference file belongs to', () => {
		expect(summarizeTools([{
			name: 'read_file', object: '/w/skills/sdlc-process/references/code-health.md',
		}])[0].label).toBe('Read sdlc-process skill (references/code-health.md)');
	});

	it('names an edited skill without inventing an intent', () => {
		expect(summarizeTools([{ name: 'edit_file', object: GITHUB_SKILL }])[0].label).toBe(
			'Edited github skill'
		);
	});

	it('recognizes a thread-local skill path', () => {
		expect(summarizeTools([{
			name: 'read_file',
			object: '/w/projects/p/.threads/af204877/skills/server-log-triage/SKILL.md',
		}])[0].label).toBe('Read server-log-triage skill');
	});

	it('keeps an ordinary file label unchanged', () => {
		expect(summarizeTools([{ name: 'Read', object: '/a/b/tool_activity.py' }])[0].label).toBe(
			'Read tool_activity.py'
		);
		expect(summarizeTools([
			{ name: 'Read', object: '/a.py' },
			{ name: 'Read', object: '/b.py' },
		])[0].label).toBe('Read 2 files');
	});

	it('keeps skill files and ordinary files in separate segments', () => {
		const segs = summarizeTools([
			{ name: 'read_file', object: TASK_SKILL },
			{ name: 'read_file', object: '/a.py' },
		]);
		expect(segs.map((s) => s.label)).toEqual(['Read task-discipline skill', 'Read a.py']);
	});

	it('treats a bare SKILL.md outside a skill folder as an ordinary file', () => {
		expect(skillLabel('/w/skills/SKILL.md')).toBeNull();
		expect(skillLabel('/w/docs/notes.md')).toBeNull();
	});
});
