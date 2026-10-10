import type { ActiveTool } from './threads';

/** A running row from a tool_start frame, carrying its backend-projected fields. */
export function startActiveTool(frame: Record<string, any>): ActiveTool {
	return {
		id: frame.tool_id,
		name: frame.name,
		hint: frame.hint,
		phrase: frame.phrase || undefined,
		object: frame.object || undefined,
		status: 'running',
		args: frame.args || undefined,
	};
}

/** Apply a tool_done frame; its phrase, from the final args, replaces a streamed one. */
export function settleActiveTool(tool: ActiveTool, frame: Record<string, any>): ActiveTool {
	return {
		...tool,
		status: 'done',
		phrase: frame.phrase || tool.phrase,
		object: frame.object || tool.object,
		outcome: frame.outcome || undefined,
		error_line: frame.error_line || undefined,
		result: frame.result || undefined,
	};
}
