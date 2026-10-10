import fs from 'node:fs';
import path from 'node:path';

import { getOption, writeJson, writeText } from './utils.mjs';

export function writeReport(runDir, report) {
	writeJson(path.join(runDir, 'report.json'), report);
	writeText(path.join(runDir, 'report.md'), renderReportMarkdown(report));
}

export function renderReportMarkdown(report) {
	const lines = [
		`# QA Run ${report.run_id}`,
		'',
		`Status: ${report.status}`,
		`Project: ${report.project}`,
		`Started: ${report.started_at}`,
		report.completed_at ? `Completed: ${report.completed_at}` : null,
		'',
		'## Selection',
		'',
		`Changed files: ${report.changed_files?.length ? report.changed_files.join(', ') : '(none detected)'}`,
		`Matched rules: ${report.matched_rules?.length ? report.matched_rules.join(', ') : '(default)'}`,
		'',
		'## Commands',
		'',
		...(report.commands ?? []).map((entry) => `- [${entry.status}] ${entry.command} (${entry.duration_ms ?? '-'} ms, log: ${entry.log})`),
		'',
	];
	if (report.llm_review) {
		lines.push('## LLM Review', '', `Status: ${report.llm_review.status}`, report.llm_review.summary ?? '', '');
	}
	return lines.filter((line) => line !== null).join('\n');
}

export function renderToolsMarkdown(payload) {
	const lines = ['# QA Tool Evaluation', '', `Generated: ${payload.generated_at}`, ''];
	for (const tool of payload.results) {
		lines.push(`## ${tool.name}`, '', `Recommendation: ${tool.recommendation}`, `License: ${tool.license}`, '', tool.fit, '');
		if (tool.risks?.length) {
			lines.push('Risks:', ...tool.risks.map((risk) => `- ${risk}`), '');
		}
		if (tool.required_spike?.length) {
			lines.push('Required spike:', ...tool.required_spike.map((step) => `- ${step}`), '');
		}
		if (tool.online?.length) {
			lines.push('Online checks:', ...tool.online.map((entry) => `- ${entry.url}: ${entry.status}`), '');
		}
	}
	return lines.join('\n');
}

export async function fetchStatus(url) {
	try {
		const response = await fetch(url, { method: 'HEAD' });
		return { url, status: response.status };
	} catch (error) {
		return { url, status: 'error', error: String(error?.message ?? error) };
	}
}

export function resolveReportPath(repoRoot, config, args) {
	const explicit = getOption(args, '--report');
	if (explicit) return path.resolve(repoRoot, explicit);
	const runId = getOption(args, '--run');
	if (runId) return path.resolve(repoRoot, config.paths.artifacts, runId, 'report.json');
	const root = path.resolve(repoRoot, config.paths.artifacts);
	const runs = fs.existsSync(root)
		? fs.readdirSync(root).filter((name) => fs.existsSync(path.join(root, name, 'report.json'))).sort()
		: [];
	if (runs.length === 0) throw new Error(`No QA reports found under ${root}`);
	return path.join(root, runs[runs.length - 1], 'report.json');
}
