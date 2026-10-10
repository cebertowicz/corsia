export type ToolKind =
	| 'read' | 'write' | 'edit' | 'exec' | 'search' | 'web'
	| 'agent' | 'todo' | 'memory' | 'generic';

const KIND_BY_NAME: Record<string, ToolKind> = {
	read_file: 'read', list_files: 'read',
	write_file: 'write',
	edit_file: 'edit', multi_edit: 'edit',
	run_command: 'exec',
	search_files: 'search', memory_search: 'memory',
	web_search: 'web', fetch_url: 'web',
	project_list: 'read',
	project_create: 'write',
	conversation_set_project: 'write',
	write_knowledge: 'write',
	search_knowledge: 'memory',
	list_knowledge: 'memory',
	delegate_create_thread: 'agent',
	delegate_send: 'agent',
	delegate_status: 'agent',
	delegate_set_model: 'agent',
	delegate_task: 'agent',
	doc_comments_list: 'read',
	doc_comment_create: 'write',
	doc_comment_update: 'write',
	doc_feedback_send: 'write',
	doc_review_request: 'write',
	artifact_resolve: 'read',
	artifact_register: 'write',
	artifact_update: 'write',
	artifact_reconcile: 'read',
	artifact_relocate: 'write',
	artifact_rehome: 'write',
	artifact_release: 'write',
	create_memo: 'memory',
	Read: 'read', NotebookRead: 'read',
	Write: 'write',
	Edit: 'edit', MultiEdit: 'edit', NotebookEdit: 'edit',
	Bash: 'exec', bash: 'exec', BashOutput: 'exec', KillShell: 'exec',
	Grep: 'search', grep: 'search', Glob: 'search', glob: 'search',
	WebSearch: 'web', WebFetch: 'web',
	Task: 'agent', Agent: 'agent',
	TodoWrite: 'todo',
};

function parseArgs(args: unknown): Record<string, any> | null {
	if (args && typeof args === 'object') return args as Record<string, any>;
	if (typeof args !== 'string' || !args) return null;
	try {
		const parsed = JSON.parse(args);
		return parsed && typeof parsed === 'object' ? parsed : null;
	} catch {
		return null;
	}
}

const CORSIA_MCP_PREFIX = 'mcp__corsia__';
const CORSIA_MCP_BRIDGE_TOOL = `${CORSIA_MCP_PREFIX}corsia_tool`;

function presentationTool(name: string, args: unknown): { name: string; args: unknown } {
	if (name !== CORSIA_MCP_BRIDGE_TOOL) {
		if (name.startsWith(CORSIA_MCP_PREFIX)) {
			return { name: name.slice(CORSIA_MCP_PREFIX.length), args };
		}
		return { name, args };
	}
	const a = parseArgs(args);
	const innerName = typeof a?.name === 'string' && a.name ? a.name : name;
	const innerArgs = a?.arguments && typeof a.arguments === 'object' ? a.arguments : {};
	return { name: innerName, args: innerArgs };
}

export function toolKind(name: string, args?: unknown): ToolKind {
	const t = presentationTool(name, args);
	return KIND_BY_NAME[t.name] ?? 'generic';
}

export function toolDisplayArgs(name: string, args: unknown): unknown {
	return presentationTool(name, args).args;
}

export function toolDetailResult(name: string, result: unknown): string | undefined {
	if (result == null) return undefined;
	const text = typeof result === 'string' ? result : String(result);
	if (name !== CORSIA_MCP_BRIDGE_TOOL) return text;
	const parsed = parseArgs(text);
	if (typeof parsed?.result === 'string') return parsed.result;
	if (typeof parsed?.error === 'string') return parsed.error;
	return text;
}

function basename(p: string): string {
	const parts = p.replace(/\/+$/, '').split('/');
	return parts[parts.length - 1] || p;
}

export interface SkillTarget {
	name: string;
	reference: string | null;
}

/**
 * The skill a path targets: `…/skills/<name>/SKILL.md`, or one of that skill's
 * `references/` files. Null for every other path — an ordinary file named
 * `SKILL.md` outside a skill folder is not a skill resource.
 */
export function skillTarget(path: string): SkillTarget | null {
	const parts = path.replace(/\\/g, '/').replace(/\/+$/, '').split('/').filter(Boolean);
	for (let i = parts.length - 3; i >= 0; i--) {
		if (parts[i] !== 'skills') continue;
		const name = parts[i + 1];
		const rest = parts.slice(i + 2);
		if (rest.length === 1 && rest[0] === 'SKILL.md') return { name, reference: null };
		if (rest.length > 1 && rest[0] === 'references') {
			return { name, reference: rest.slice(1).join('/') };
		}
	}
	return null;
}

/** The label a skill resource earns: its name, plus the reference file it names. */
export function skillLabel(path: string): string | null {
	const target = skillTarget(path);
	if (!target) return null;
	const reference = target.reference ? ` (references/${target.reference})` : '';
	return `${target.name} skill${reference}`;
}

export type ToolOutcome = 'done' | 'failed' | 'refused' | 'unknown';

/**
 * Format the backend-projected outcome; the backend picks the error line.
 * Empty when the call claims no outcome: running, or no result stored.
 */
export function toolOutcomeLabel(
	outcome: ToolOutcome | null | undefined, errorLine?: string | null
): string {
	if (!outcome) return '';
	if (outcome === 'unknown') return 'Outcome unknown';
	return outcome === 'failed' && errorLine ? `failed — ${errorLine}` : outcome;
}

export interface SummarySegment {
	kind: ToolKind;
	count: number;
	label: string;
}

const PLURAL: Record<ToolKind, (n: number, single: string) => string> = {
	read: (n, s) => (n === 1 ? `Read ${s}` : `Read ${n} files`),
	write: (n, s) => (n === 1 ? `Wrote ${s}` : `Wrote ${n} files`),
	edit: (n, s) => (n === 1 ? `Edited ${s}` : `Edited ${n} files`),
	exec: (n, s) => (n === 1 ? (s ? `Ran ${s}` : 'Ran a command') : `Ran ${n} commands`),
	search: (n, s) => (n === 1 ? (s ? `Searched ${s}` : 'Searched once') : `Searched ${n} times`),
	web: (n) => (n === 1 ? 'Searched the web' : `Searched the web ${n}×`),
	agent: (n) => (n === 1 ? 'Ran an agent' : `Ran ${n} agents`),
	todo: () => 'Updated todos',
	memory: (n) => (n === 1 ? 'Searched memory' : `Searched memory ${n}×`),
	generic: (n) => (n === 1 ? 'Used a tool' : `Used ${n} tools`),
};

const SKILL_VERB: Record<'read' | 'write' | 'edit', string> = {
	read: 'Read', write: 'Wrote', edit: 'Edited',
};

/**
 * One segment's label for the skills a block consulted: a single skill is
 * named, several are listed by name so no two skill rows read alike.
 */
function skillSummary(kind: 'read' | 'write' | 'edit', skills: string[]): string {
	if (skills.length === 1) return PLURAL[kind](1, skillLabel(skills[0]) ?? '');
	const names = [...new Set(skills.map((p) => skillTarget(p)!.name))];
	if (names.length === 1) return PLURAL[kind](1, `${names[0]} skill`);
	return `${SKILL_VERB[kind]} ${names.length} skills: ${names.join(', ')}`;
}

/** Summarize a block by kind; each call's object is the one the backend projected. */
export function summarizeTools(
	tools: { name: string; args?: unknown; object?: string }[]
): SummarySegment[] {
	const byKind = new Map<ToolKind, { count: number; objects: Set<string> }>();
	for (const t of tools) {
		const kind = toolKind(t.name, t.args);
		const entry = byKind.get(kind) ?? { count: 0, objects: new Set<string>() };
		const obj = t.object ?? '';
		if (kind === 'read' || kind === 'write' || kind === 'edit') {
			if (obj) entry.objects.add(obj);
			entry.count = entry.objects.size || entry.count + (obj ? 0 : 1);
		} else {
			if ((kind === 'exec' || kind === 'search') && obj) entry.objects.add(obj);
			entry.count += 1;
		}
		byKind.set(kind, entry);
	}
	const out: SummarySegment[] = [];
	for (const [kind, e] of byKind) {
		const n = Math.max(1, e.count);
		const isFileKind = ['read', 'write', 'edit'].includes(kind);
		const objects = [...e.objects];
		const skills = isFileKind ? objects.filter((o) => skillTarget(o)) : [];
		const files = isFileKind ? objects.filter((o) => !skillTarget(o)) : objects;
		if (skills.length) {
			out.push({ kind, count: skills.length, label: skillSummary(kind as 'read', skills) });
			if (!files.length) continue;
		}
		const count = skills.length ? files.length : n;
		const onlyObject = files.length === 1 ? files[0] : '';
		const single = isFileKind ? basename(onlyObject) : onlyObject;
		out.push({ kind, count, label: PLURAL[kind](Math.max(1, count), single) });
	}
	return out;
}

export function diffPayload(
	name: string, args: unknown
): { file: string; oldText: string; newText: string } | null {
	const displayArgs = toolDisplayArgs(name, args);
	const kind = toolKind(name, args);
	if (kind !== 'edit' && kind !== 'write') return null;
	const a = parseArgs(displayArgs);
	if (!a) return null;
	const file = a.file_path ?? a.path ?? a.notebook_path ?? '';
	if (kind === 'edit') {
		const oldText = a.old_string ?? a.old_text;
		const newText = a.new_string ?? a.new_text;
		if (typeof oldText !== 'string' || typeof newText !== 'string') return null;
		return { file, oldText, newText };
	}
	if (typeof a.content !== 'string') return null;
	return { file, oldText: '', newText: a.content };
}
