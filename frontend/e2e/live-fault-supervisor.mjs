#!/usr/bin/env node
/**
 * Test-only E2E child supervisor.
 *
 * It owns one uvicorn child. It never discovers or kills a listener by port;
 * after one expected SIGKILL whose marker names that exact child, it restarts
 * the child over the same workspace/state roots. Every other exit fails.
 */
import { spawn } from 'node:child_process';
import crypto from 'node:crypto';
import fs from 'node:fs';
import path from 'node:path';
import process from 'node:process';
import { fileURLToPath } from 'node:url';

function required(environment, name) {
	const value = environment[name];
	if (!value) throw new Error(`live-fault supervisor requires ${name}`);
	return value;
}

function requiredOne(environment, name) {
	if (environment[name] !== '1') throw new Error(`live-fault supervisor requires ${name}=1`);
}

function inside(parent, candidate) {
	const relative = path.relative(parent, candidate);
	return relative && !relative.startsWith(`..${path.sep}`) && relative !== '..' && !path.isAbsolute(relative);
}

export function preflight(environment = process.env) {
	for (const name of ['CORSIA_E2E_LIVE_FAULTS', 'CORSIA_TEST_MODE', 'CORSIA_E2E_MOCK', 'CORSIA_E2E_FRESH_ROOTS']) requiredOne(environment, name);
	const workspaceText = required(environment, 'CORSIA_WORKSPACE');
	const stateDirText = required(environment, 'CORSIA_STATE_DIR');
	const workspace = path.resolve(workspaceText);
	const stateDir = path.resolve(stateDirText);
	const backendPort = Number(required(environment, 'CORSIA_E2E_BACKEND_PORT'));
	const python = required(environment, 'CORSIA_E2E_BACKEND_PYTHON');
	if (!Number.isInteger(backendPort) || backendPort < 1024 || backendPort > 65535 || [3000, 3001].includes(backendPort)) {
		throw new Error('live-fault supervisor requires an isolated non-serving backend port');
	}
	if (!path.isAbsolute(workspaceText) || !path.isAbsolute(stateDirText) || workspace === stateDir || inside(workspace, stateDir) || inside(stateDir, workspace)) {
		throw new Error('live-fault supervisor requires distinct absolute workspace and state roots');
	}
	if (!fs.statSync(workspace).isDirectory() || !fs.statSync(stateDir).isDirectory()
		|| fs.realpathSync(workspace) !== workspace || fs.realpathSync(stateDir) !== stateDir) {
		throw new Error('live-fault supervisor requires prepared roots with no symlinked ancestor');
	}
	const runId = crypto.randomUUID();
	const runDir = fs.mkdtempSync(path.join(stateDir, 'live-fault-run-'));
	fs.writeFileSync(path.join(runDir, 'identity.json'), JSON.stringify({ run_id: runId, workspace, state_dir: stateDir }), { encoding: 'utf8', mode: 0o600 });
	fs.writeFileSync(path.join(runDir, 'restart.json'), JSON.stringify({ case_id: null, restart_count: 0 }), { encoding: 'utf8', mode: 0o600 });
	return { backendPort, python, runDir, runId, workspace, stateDir };
}

export function readExpectedArm(markerPath, runId, pid) {
	try {
		const arm = JSON.parse(fs.readFileSync(markerPath, 'utf8'));
		const state = JSON.parse(fs.readFileSync(path.join(path.dirname(markerPath), 'restart.json'), 'utf8'));
		return arm.run_id === runId && arm.pid === pid && arm.generation === 1
			&& typeof arm.case_id === 'string' && arm.case_id.length > 0 && arm.case_id === state.case_id && state.restart_count === 0
			&& typeof arm.artifact_id === 'string' && arm.artifact_id.length > 0
			&& ['choice_record', 'choice_rename', 'choice_apply', 'choice_clear'].includes(arm.stage) ? arm : null;
	} catch (error) {
		if (error?.code === 'ENOENT') return null;
		throw new Error(`live-fault marker is unreadable: ${error}`);
	}
}

/** Isolated state machine so its bounded restart rule has direct unit proof. */
export class LiveFaultSupervisor {
	constructor({ spawnChild, inspectArm, clearArm, recordRestart = () => {}, onFatal, onStopped = () => {} }) {
		this.spawnChild = spawnChild;
		this.inspectArm = inspectArm;
		this.clearArm = clearArm;
		this.recordRestart = recordRestart;
		this.onFatal = onFatal;
		this.onStopped = onStopped;
		this.restartedCases = new Set();
		this.stopping = false;
		this.child = null;
	}

	start() {
		const child = this.spawnChild();
		this.child = child;
		child.once('error', error => this.onFatal(error));
		child.once('exit', (code, signal) => this.onChildExit(child, code, signal));
		return child;
	}

	onChildExit(child, code, signal) {
		if (child !== this.child) {
			this.onFatal(new Error('live-fault exit did not belong to the owned current child'));
			return;
		}
		if (this.stopping) {
			this.onStopped();
			return;
		}
		const arm = this.inspectArm(child.pid);
		if (arm && !this.restartedCases.has(arm.case_id) && code === null && signal === 'SIGKILL') {
			this.clearArm();
			this.restartedCases.add(arm.case_id);
			this.recordRestart({ case_id: arm.case_id, restart_count: 1, prior_pid: child.pid, stage: arm.stage });
			this.start();
			return;
		}
		this.onFatal(new Error(`live-fault child pid=${child.pid} exited code=${code} signal=${signal}; expected one armed SIGKILL only`));
	}

	shutdown() {
		this.stopping = true;
		if (!this.child || this.child.exitCode !== null || this.child.signalCode !== null) return;
		this.child.kill('SIGTERM');
		setTimeout(() => {
			if (this.child && this.child.exitCode === null && this.child.signalCode === null) this.child.kill('SIGKILL');
		}, 5_000).unref();
	}
}

function main() {
	const settings = preflight();
	const markerPath = path.join(settings.runDir, 'armed-cut.json');
	const childEnvironment = {
		...process.env,
		CORSIA_E2E_LIVE_FAULT_RUN_ID: settings.runId,
		CORSIA_E2E_LIVE_FAULT_RUN_DIR: settings.runDir,
	};
	const spawnChild = () => spawn(settings.python, ['-m', 'uvicorn', 'e2e_app:app', '--host', '127.0.0.1', '--port', settings.backendPort], {
		cwd: path.resolve(path.dirname(fileURLToPath(import.meta.url)), '../../backend'),
		env: childEnvironment,
		stdio: 'inherit',
	});
	let supervisor;
	const terminate = (error) => {
		console.error(error.message);
		supervisor.stopping = true;
		process.exitCode = 1;
	};
	supervisor = new LiveFaultSupervisor({
		spawnChild,
		inspectArm: pid => readExpectedArm(markerPath, settings.runId, pid),
		clearArm: () => fs.rmSync(markerPath, { force: true }),
		recordRestart: record => {
			const temporary = path.join(settings.runDir, 'restart.tmp');
			fs.writeFileSync(temporary, JSON.stringify(record), { encoding: 'utf8', mode: 0o600 });
			fs.renameSync(temporary, path.join(settings.runDir, 'restart.json'));
		},
		onFatal: terminate,
		onStopped: () => {
			fs.rmSync(settings.runDir, { recursive: true, force: true });
			process.exit(process.exitCode ?? 0);
		},
	});
	for (const signal of ['SIGINT', 'SIGTERM']) process.once(signal, () => supervisor.shutdown());
	supervisor.start();
}

if (process.argv[1] && import.meta.url === new URL(`file://${process.argv[1]}`).href) main();
