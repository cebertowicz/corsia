import fs from 'node:fs';
import path from 'node:path';

import { readOptional, truncate, unique, walk } from './utils.mjs';

export async function runLlmReview(repoRoot, config, report, runDir) {
	const llm = config.llm ?? {};
	const baseUrl = process.env[llm.baseUrlEnv ?? 'QA_LLM_BASE_URL'];
	const model = process.env[llm.modelEnv ?? 'QA_LLM_MODEL'] ?? llm.defaultModel ?? 'local-vision';
	if (!baseUrl) {
		return {
			status: 'skipped',
			surface: 'unknown',
			summary: `Set ${llm.baseUrlEnv ?? 'QA_LLM_BASE_URL'} to enable local LLM UX review.`,
			findings: [],
			questions: [],
		};
	}

	const prompt = readOptional(repoRoot, config.paths.reviewPrompt);
	const designSystem = truncate(readOptional(repoRoot, config.paths.designSystem), 12000);
	const artifactPaths = collectArtifacts(repoRoot, config, runDir);
	const images = artifactPaths
		.filter((artifact) => artifact.endsWith('.png') || artifact.endsWith('.jpg') || artifact.endsWith('.jpeg'))
		.slice(0, llm.maxImages ?? 8);
	const userText = [
		prompt,
		'QA run report:',
		JSON.stringify(report, null, 2),
		'Design-system context:',
		designSystem,
		'Artifact paths:',
		artifactPaths.join('\n') || '(none)',
	].filter(Boolean).join('\n\n');
	const content = [{ type: 'text', text: userText }];
	for (const image of images) {
		content.push({
			type: 'image_url',
			image_url: {
				url: `data:image/${image.endsWith('.jpg') || image.endsWith('.jpeg') ? 'jpeg' : 'png'};base64,${fs.readFileSync(path.resolve(repoRoot, image)).toString('base64')}`,
			},
		});
	}

	const headers = { 'Content-Type': 'application/json' };
	const apiKey = process.env[llm.apiKeyEnv ?? 'QA_LLM_API_KEY'];
	if (apiKey) headers.Authorization = `Bearer ${apiKey}`;

	const response = await fetch(`${baseUrl.replace(/\/$/, '')}/chat/completions`, {
		method: 'POST',
		headers,
		body: JSON.stringify({
			model,
			temperature: 0.1,
			response_format: { type: 'json_object' },
			messages: [
				{ role: 'system', content: 'Return only JSON matching the requested QA review schema.' },
				{ role: 'user', content },
			],
		}),
	});
	const raw = await response.text();
	if (!response.ok) {
		return {
			status: 'warn',
			surface: 'unknown',
			summary: `Local LLM review endpoint returned HTTP ${response.status}.`,
			findings: [{ severity: 'medium', kind: 'review-runtime', evidence: raw.slice(0, 1000), recommendation: 'Check QA_LLM_BASE_URL, model name, and endpoint compatibility.' }],
			questions: [],
		};
	}
	const data = JSON.parse(raw);
	return parseJsonReview(data.choices?.[0]?.message?.content ?? '');
}

export function collectArtifacts(repoRoot, config, runDir) {
	const roots = [runDir, ...(config.artifactInputs ?? []).map((p) => path.resolve(repoRoot, p))];
	const files = [];
	for (const root of roots) {
		if (!fs.existsSync(root)) continue;
		for (const file of walk(root)) {
			if (/\.(png|jpe?g|zip|json|md|html|webm)$/i.test(file)) files.push(path.relative(repoRoot, file));
		}
	}
	return unique(files).sort();
}

function parseJsonReview(value) {
	try {
		return JSON.parse(value);
	} catch {
		const start = value.indexOf('{');
		const end = value.lastIndexOf('}');
		if (start >= 0 && end > start) {
			try {
				return JSON.parse(value.slice(start, end + 1));
			} catch {
				// Return the structured warning below.
			}
		}
		return {
			status: 'warn',
			surface: 'unknown',
			summary: 'Local LLM review did not return parseable JSON.',
			findings: [{ severity: 'medium', kind: 'review-runtime', evidence: truncate(value, 1000), recommendation: 'Tighten the review prompt or model JSON mode.' }],
			questions: [],
		};
	}
}
