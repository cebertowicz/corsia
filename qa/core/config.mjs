import fs from 'node:fs';
import path from 'node:path';

import { getOption, globToRegExp, unique } from './utils.mjs';

export function loadConfig(repoRoot, args) {
	const configPath = getOption(args, '--config') ?? 'qa.config.json';
	const absolute = path.resolve(repoRoot, configPath);
	if (!fs.existsSync(absolute)) throw new Error(`QA config not found: ${absolute}`);
	const config = JSON.parse(fs.readFileSync(absolute, 'utf8'));
	config.__path = absolute;
	return config;
}

export function loadImpactMap(repoRoot, config) {
	const mapPath = path.resolve(repoRoot, config.paths.impactMap);
	if (!fs.existsSync(mapPath)) throw new Error(`Impact map not found: ${mapPath}`);
	return JSON.parse(fs.readFileSync(mapPath, 'utf8'));
}

export function selectImpacts(map, changed) {
	const matches = [];
	for (const rule of map.rules ?? []) {
		const globs = rule.globs ?? [];
		if (changed.some((file) => globs.some((glob) => globToRegExp(glob).test(file)))) {
			matches.push(rule);
		}
	}
	const commands = unique(matches.flatMap((rule) => normalizeCommands(rule.commands)));
	const defaults = map.default ?? [];
	const fallback = unique(defaults.flatMap((entry) => normalizeCommands(entry.command ?? entry.commands)));
	const always = unique(
		defaults.filter((entry) => entry.always).flatMap((entry) => normalizeCommands(entry.command ?? entry.commands)),
	);
	return { matches, commands: commands.length > 0 ? unique([...always, ...commands]) : fallback };
}

function normalizeCommands(value) {
	if (!value) return [];
	return Array.isArray(value) ? value : [value];
}
