#!/usr/bin/env node
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';

import { buildCatalog, renderCatalogMarkdown } from './catalog.mjs';
import { loadConfig, loadImpactMap, selectImpacts } from './config.mjs';
import { renderReportMarkdown, renderToolsMarkdown, resolveReportPath, writeReport, fetchStatus } from './reporting.mjs';
import { collectArtifacts, runLlmReview } from './review.mjs';
import { ensureDir, explicitFiles, findRepoRoot, getChangedFiles, hasFlag, makeRunId, normalizePath, runShell, writeJson, writeText } from './utils.mjs';

const repoRoot = findRepoRoot(process.cwd());

main().catch((error) => {
	console.error(error?.stack || String(error));
	process.exitCode = 1;
});

async function main() {
	const [command = 'help', ...args] = process.argv.slice(2);
	switch (command) {
		case 'select':
			return selectCommand(args);
		case 'run':
			return runCommand(args);
		case 'catalog':
			return catalogCommand(args);
		case 'review':
			return reviewCommand(args);
		case 'eval-tools':
			return evalToolsCommand(args);
		case 'report':
			return reportCommand(args);
		case 'help':
		case '--help':
		case '-h':
			return printHelp();
		default:
			throw new Error(`Unknown qa command: ${command}`);
	}
}

function printHelp() {
	console.log(`Usage: node qa/core/cli.mjs <command> [options]

Commands:
  select [--json] [files...]        Select impacted test commands.
  run [--dry-run] [--json] [files...] Run impacted test commands and write a local report.
  catalog [--json] [--write]        Inventory configured routes, components, and API endpoints.
  review [--run <id>|--report <path>] Review a run with a local OpenAI-compatible model.
  eval-tools [--json] [--online]    Emit the OSS/tool evaluation matrix.
  report [--run <id>]               Print a saved local report.

Common options:
  --config <path>                   Defaults to qa.config.json.
`);
}

function selectCommand(args) {
	const config = loadConfig(repoRoot, args);
	const map = loadImpactMap(repoRoot, config);
	const files = explicitFiles(args);
	const changed = files.length > 0 ? files.map(normalizePath) : getChangedFiles(repoRoot);
	const selection = selectImpacts(map, changed);
	const payload = {
		project: config.projectName,
		adapter: config.adapter,
		changed,
		matchedRules: selection.matches.map((rule) => rule.id),
		commands: selection.commands,
	};

	if (hasFlag(args, '--json')) {
		console.log(JSON.stringify(payload, null, 2));
		return;
	}

	console.log(`Project: ${payload.project}`);
	console.log(`Changed files: ${changed.length === 0 ? '(none detected)' : changed.join(', ')}`);
	console.log(`Matched rules: ${payload.matchedRules.length === 0 ? '(default)' : payload.matchedRules.join(', ')}`);
	console.log('\nRecommended commands:');
	for (const command of payload.commands) console.log(`  ${command}`);
}

async function runCommand(args) {
	const config = loadConfig(repoRoot, args);
	const map = loadImpactMap(repoRoot, config);
	const files = explicitFiles(args);
	const changed = files.length > 0 ? files.map(normalizePath) : getChangedFiles(repoRoot);
	const selection = selectImpacts(map, changed);
	const runId = makeRunId();
	const runDir = path.resolve(repoRoot, config.paths.artifacts, runId);
	const dryRun = hasFlag(args, '--dry-run');
	const report = {
		run_id: runId,
		project: config.projectName,
		adapter: config.adapter,
		started_at: new Date().toISOString(),
		completed_at: null,
		status: dryRun ? 'dry-run' : 'running',
		changed_files: changed,
		matched_rules: selection.matches.map((rule) => rule.id),
		commands: selection.commands.map((command, index) => ({
			index: index + 1,
			command,
			status: dryRun ? 'skipped' : 'pending',
			exit_code: null,
			duration_ms: null,
			log: `command-${String(index + 1).padStart(2, '0')}.log`,
		})),
		artifacts: [],
		llm_review: null,
	};

	if (dryRun) {
		if (hasFlag(args, '--json')) console.log(JSON.stringify(report, null, 2));
		else {
			console.log(`Dry run ${runId}`);
			for (const entry of report.commands) console.log(`  ${entry.command}`);
		}
		return;
	}

	ensureDir(runDir);
	writeJson(path.join(runDir, 'selection.json'), {
		changed,
		matchedRules: report.matched_rules,
		commands: selection.commands,
	});

	let failed = false;
	for (const entry of report.commands) {
		const logPath = path.join(runDir, entry.log);
		const started = Date.now();
		entry.status = 'running';
		writeReport(runDir, report);
		const exitCode = await runShell(entry.command, repoRoot, logPath);
		entry.exit_code = exitCode;
		entry.duration_ms = Date.now() - started;
		entry.status = exitCode === 0 ? 'passed' : 'failed';
		if (exitCode !== 0) failed = true;
		writeReport(runDir, report);
	}

	report.completed_at = new Date().toISOString();
	report.status = failed ? 'failed' : 'passed';
	report.artifacts = collectArtifacts(repoRoot, config, runDir);
	writeReport(runDir, report);

	if (hasFlag(args, '--json')) console.log(JSON.stringify(report, null, 2));
	else console.log(`QA run ${runId}: ${report.status} (${path.relative(repoRoot, runDir)}/report.md)`);
	if (failed) process.exitCode = 1;
}

function catalogCommand(args) {
	const config = loadConfig(repoRoot, args);
	const catalog = buildCatalog(repoRoot, config);

	if (hasFlag(args, '--write')) {
		const runDir = path.resolve(repoRoot, config.paths.artifacts, `catalog-${makeRunId()}`);
		ensureDir(runDir);
		writeJson(path.join(runDir, 'catalog.json'), catalog);
		writeText(path.join(runDir, 'catalog.md'), renderCatalogMarkdown(catalog));
		if (!hasFlag(args, '--json')) console.log(`Catalog written to ${path.relative(repoRoot, runDir)}`);
	}

	if (hasFlag(args, '--json')) console.log(JSON.stringify(catalog, null, 2));
	else if (!hasFlag(args, '--write')) console.log(renderCatalogMarkdown(catalog));
}

async function reviewCommand(args) {
	const config = loadConfig(repoRoot, args);
	const reportPath = resolveReportPath(repoRoot, config, args);
	const report = JSON.parse(fs.readFileSync(reportPath, 'utf8'));
	const runDir = path.dirname(reportPath);
	const review = await runLlmReview(repoRoot, config, report, runDir);
	report.llm_review = review;
	writeJson(path.join(runDir, 'review.json'), review);
	writeReport(runDir, report);
	console.log(JSON.stringify(review, null, 2));
	if (review.status === 'fail') process.exitCode = 1;
}

async function evalToolsCommand(args) {
	const config = loadConfig(repoRoot, args);
	const candidatesPath = path.resolve(repoRoot, config.paths.toolCandidates);
	const candidates = JSON.parse(fs.readFileSync(candidatesPath, 'utf8'));
	const online = hasFlag(args, '--online');
	const results = [];
	for (const candidate of candidates.tools) {
		const result = {
			id: candidate.id,
			name: candidate.name,
			license: candidate.license,
			recommendation: candidate.recommendation,
			fit: candidate.fit,
			risks: candidate.risks,
			required_spike: candidate.required_spike,
			urls: candidate.urls,
			online: [],
		};
		if (online) {
			for (const url of candidate.urls ?? []) {
				result.online.push(await fetchStatus(url));
			}
		}
		results.push(result);
	}
	const payload = {
		generated_at: new Date().toISOString(),
		status: 'advisory',
		results,
	};
	if (hasFlag(args, '--json')) console.log(JSON.stringify(payload, null, 2));
	else console.log(renderToolsMarkdown(payload));
}

function reportCommand(args) {
	const config = loadConfig(repoRoot, args);
	const reportPath = resolveReportPath(repoRoot, config, args);
	const report = JSON.parse(fs.readFileSync(reportPath, 'utf8'));
	if (hasFlag(args, '--json')) console.log(JSON.stringify(report, null, 2));
	else console.log(renderReportMarkdown(report));
}
