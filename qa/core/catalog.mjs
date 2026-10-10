import fs from 'node:fs';
import path from 'node:path';

import { normalizePath, walk } from './utils.mjs';

export function buildCatalog(repoRoot, config) {
	return {
		project: config.projectName,
		generated_at: new Date().toISOString(),
		routes: collectFiles(repoRoot, config.catalog?.routes ?? []).map((file) => ({
			path: file,
			route: routeFromFile(file),
		})),
		components: collectFiles(repoRoot, config.catalog?.components ?? [])
			.filter((file) => file.endsWith('.svelte'))
			.map((file) => ({ path: file, name: path.basename(file, '.svelte') })),
		api_endpoints: collectApiEndpoints(repoRoot, config.catalog?.api ?? []),
	};
}

export function collectFiles(repoRoot, roots) {
	return roots.flatMap((root) => fs.existsSync(path.resolve(repoRoot, root)) ? walk(path.resolve(repoRoot, root)) : [])
		.map((file) => normalizePath(path.relative(repoRoot, file)))
		.sort();
}

export function collectApiEndpoints(repoRoot, roots) {
	const endpoints = [];
	for (const file of collectFiles(repoRoot, roots)) {
		if (!file.endsWith('.py')) continue;
		const absolute = path.resolve(repoRoot, file);
		const lines = fs.readFileSync(absolute, 'utf8').split('\n');
		for (let index = 0; index < lines.length; index++) {
			const match = lines[index].match(/@(?:router|app)\.(get|post|patch|delete|put|websocket)\("([^"]*)"/);
			if (match) {
				endpoints.push({
					method: match[1].toUpperCase(),
					path: match[2],
					file,
					line: index + 1,
				});
			}
		}
	}
	return endpoints;
}

export function routeFromFile(file) {
	return file
		.replace(/^frontend\/src\/routes/, '')
		.replace(/\/\+page\.svelte$/, '/')
		.replace(/\/\+layout\.svelte$/, '/(layout)')
		.replace(/\/\+page\.ts$/, '/')
		.replace(/\/\+layout\.ts$/, '/(layout)')
		.replace(/\/$/, '') || '/';
}

export function renderCatalogMarkdown(catalog) {
	return [
		`# ${catalog.project} QA Catalog Inventory`,
		'',
		`Generated: ${catalog.generated_at}`,
		'',
		`Routes: ${catalog.routes.length}`,
		`Components: ${catalog.components.length}`,
		`API endpoints: ${catalog.api_endpoints.length}`,
		'',
		'## API Endpoints',
		'',
		...catalog.api_endpoints.map((endpoint) => `- ${endpoint.method} ${endpoint.path} (${endpoint.file}:${endpoint.line})`),
	].join('\n');
}
