import { execFileSync } from 'node:child_process';
import { resolve } from 'node:path';

// Both Playwright and Vitest invoke this helper from frontend/.
const root = resolve(process.cwd(), '..');
const backend = resolve(root, 'backend');
const program = [
	'import json, sys',
	'from llm.local_setup_results import setup_fix',
	'payload = json.loads(sys.argv[1])',
	'print(json.dumps(setup_fix(payload["cause"], payload.get("bytes_to_free"))))',
].join('; ');

/** Read the recovery copy from the backend's canonical setup_fix implementation. */
export function localSetupFix(cause: string, bytesToFree?: number): string {
	const python = process.env.CORSIA_E2E_PYTHON ?? resolve(root, '.venv/bin/python');
	return JSON.parse(execFileSync(python, ['-c', program, JSON.stringify({ cause, bytes_to_free: bytesToFree })], { cwd: backend, encoding: 'utf8' })) as string;
}
