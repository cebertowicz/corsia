import { execFileSync, spawn } from 'node:child_process';
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';

export function getChangedFiles(repoRoot) {
	const files = new Set();
	for (const args of [
		['diff', '--name-only', '--diff-filter=ACDMRT', 'HEAD'],
		['diff', '--name-only', '--diff-filter=ACDMRT', '--cached'],
		['ls-files', '--others', '--exclude-standard'],
	]) {
		const output = execFileSync('git', args, { cwd: repoRoot, encoding: 'utf8' }).trim();
		for (const line of output.split('\n').filter(Boolean)) files.add(normalizePath(line));
	}
	return [...files];
}

export function runShell(command, cwd, logPath) {
	return new Promise((resolve) => {
		const child = spawn(command, { cwd, shell: true, env: process.env });
		const log = fs.createWriteStream(logPath, { flags: 'a' });
		log.write(`$ ${command}\n`);
		child.stdout.on('data', (chunk) => {
			process.stdout.write(chunk);
			log.write(chunk);
		});
		child.stderr.on('data', (chunk) => {
			process.stderr.write(chunk);
			log.write(chunk);
		});
		child.on('close', (code) => {
			log.write(`\n[exit ${code ?? 1}]\n`);
			log.end();
			resolve(code ?? 1);
		});
	});
}

export function walk(root) {
	const out = [];
	const entries = fs.readdirSync(root, { withFileTypes: true });
	for (const entry of entries) {
		if (['node_modules', '.git', '.svelte-kit', '__pycache__'].includes(entry.name)) continue;
		const absolute = path.join(root, entry.name);
		if (entry.isDirectory()) out.push(...walk(absolute));
		else out.push(absolute);
	}
	return out;
}

export function explicitFiles(args) {
	const files = [];
	for (let i = 0; i < args.length; i++) {
		const arg = args[i];
		if (arg === '--config' || arg === '--run' || arg === '--report') {
			i++;
			continue;
		}
		if (arg.startsWith('--')) continue;
		files.push(arg);
	}
	return files;
}

export function getOption(args, name) {
	const index = args.indexOf(name);
	return index >= 0 ? args[index + 1] : undefined;
}

export function hasFlag(args, name) {
	return args.includes(name);
}

export function normalizePath(file) {
	return file.replaceAll(path.sep, '/').replace(/^\.\//, '');
}

export function unique(values) {
	return [...new Set(values)];
}

export function ensureDir(dir) {
	fs.mkdirSync(dir, { recursive: true });
}

export function writeJson(file, value) {
	ensureDir(path.dirname(file));
	fs.writeFileSync(file, `${JSON.stringify(value, null, 2)}\n`);
}

export function writeText(file, value) {
	ensureDir(path.dirname(file));
	fs.writeFileSync(file, value.endsWith('\n') ? value : `${value}\n`);
}

export function readOptional(repoRoot, relativePath) {
	if (!relativePath) return '';
	const absolute = path.resolve(repoRoot, relativePath);
	return fs.existsSync(absolute) ? fs.readFileSync(absolute, 'utf8') : '';
}

export function truncate(value, max) {
	return value.length > max ? `${value.slice(0, max)}\n[truncated]` : value;
}

export function makeRunId() {
	return new Date().toISOString().replace(/[:.]/g, '-');
}

export function globToRegExp(glob) {
	const normalized = normalizePath(glob);
	let pattern = '';
	for (let i = 0; i < normalized.length; i++) {
		const char = normalized[i];
		if (char === '*' && normalized[i + 1] === '*') {
			if (normalized[i + 2] === '/') {
				pattern += '(?:.*/)?';
				i += 2;
			} else {
				pattern += '.*';
				i++;
			}
		} else if (char === '*') {
			pattern += '[^/]*';
		} else {
			pattern += escapeRegExp(char);
		}
	}
	return new RegExp(`^${pattern}$`);
}

export function escapeRegExp(value) {
	return value.replace(/[|\\{}()[\]^$+?.]/g, '\\$&');
}

export function findRepoRoot(start) {
	let dir = path.resolve(start);
	while (dir !== path.dirname(dir)) {
		if (fs.existsSync(path.join(dir, '.git'))) return dir;
		dir = path.dirname(dir);
	}
	return path.resolve(start);
}
