import { execFile } from 'node:child_process';
import { promises as fs } from 'node:fs';
import path from 'node:path';
import { promisify } from 'node:util';

import type { TestInfo } from '@playwright/test';

const run = promisify(execFile);

const BACKEND_PORT = Number(process.env.CORSIA_E2E_BACKEND_PORT ?? 3110);

async function capture(label: string, command: string, args: string[]): Promise<string> {
	try {
		const { stdout, stderr } = await run(command, args, { timeout: 5_000 });
		return `$ ${label}\n${stdout}${stderr}`;
	} catch (error) {
		return `$ ${label}\n(${(error as Error).message})\n`;
	}
}

async function readProc(file: string): Promise<string> {
	try {
		return `${file}: ${(await fs.readFile(file, 'utf8')).trim()}\n`;
	} catch {
		return `${file}: unreadable\n`;
	}
}

/**
 * When a test times out or fails on a Linux runner, writes the sockets and kernel TCP counters the test's backend
 * port saw to `network-diagnostics.txt` in the test's output folder. A direct request to the backend that hangs
 * without an access-log line and without an error is either waiting for a connection, written to a socket the
 * backend never read, or read and never answered. The state of the client's socket (SYN-SENT with retransmits,
 * ESTAB with unsent bytes), the listener's accept queue and the drop counters tell which. This runs while the hung
 * request's socket still exists, because the request fixture is torn down after it.
 */
export async function recordNetworkDiagnostics(testInfo: TestInfo): Promise<void> {
	if (process.platform !== 'linux') return;
	if (testInfo.status !== 'timedOut' && testInfo.status !== 'failed') return;
	const sections = await Promise.all([
		capture(`ss -tanoi sockets of :${BACKEND_PORT}`, 'ss', [
			'-tanoi', '(', 'sport', '=', `:${BACKEND_PORT}`, 'or', 'dport', '=', `:${BACKEND_PORT}`, ')',
		]),
		capture('ss -s', 'ss', ['-s']),
		capture('nstat -as', 'nstat', ['-as']),
		Promise.all([
			readProc('/proc/sys/net/netfilter/nf_conntrack_count'),
			readProc('/proc/sys/net/netfilter/nf_conntrack_max'),
			readProc('/proc/sys/net/ipv4/ip_local_port_range'),
			readProc('/proc/sys/net/ipv4/tcp_tw_reuse'),
			readProc('/proc/sys/net/core/somaxconn'),
		]).then((lines) => lines.join('')),
	]);
	const report = `${new Date().toISOString()} ${testInfo.titlePath.join(' > ')} (${testInfo.status})\n\n${sections.join('\n')}`;
	await fs.mkdir(testInfo.outputDir, { recursive: true });
	await fs.writeFile(path.join(testInfo.outputDir, 'network-diagnostics.txt'), report);
	console.log(`[network-diagnostics] ${testInfo.titlePath.join(' > ')}\n${sections[0]}${sections[3]}`);
}
