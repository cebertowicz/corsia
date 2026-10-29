"""Failure details for clipboard tests on their synthetic, owned browser pages."""
import asyncio
import json
import logging
import os
import re
import shutil
import time

import psutil
import pytest

from host_access import browser_clipboard as clipboard


class _ClipboardTrace(logging.Handler):
    def __init__(self, socket_dir=None):
        super().__init__()
        self.socket_dir = socket_dir
        self.events = []
        self.endpoints = set()
        self.pointer_endpoints = set()
        self.pointer_calls = []
        self.original_call = clipboard._call
        self.original_selection = clipboard.clipboard_selection
        self.original_paste = clipboard.clipboard_paste
        self.launch_time = None

    def emit(self, record):
        self.events.append({"cdp_error": record.getMessage()})

    async def call(self, cdp, method, step, **params):
        self.endpoints.add(cdp.endpoint)
        entry = {"method": method, "step": step, "endpoint": cdp.endpoint}
        self.events.append(entry)
        started = time.monotonic()
        if method == 'Input.dispatchKeyEvent' and step in ('copy key down', 'cut key down'):
            entry['before_key'] = await _before_key(cdp, self.launch_time, self.socket_dir)
        try:
            result = await self.original_call(cdp, method, step, **params)
            if method == "Runtime.evaluate":
                entry["response"] = result
            return result
        except Exception as exc:
            entry["error"] = repr(exc)
            raise
        finally:
            entry["seconds"] = round(time.monotonic() - started, 6)

    async def action(self, name, operation, *args):
        try:
            return await operation(*args)
        except Exception as exc:
            self.events.append({"action": name, "error": repr(exc)})
            raise

    async def selection(self, endpoint, cut):
        return await self.action("copy/cut", self.original_selection, endpoint, cut)

    async def paste(self, endpoint, text):
        return await self.action("paste", self.original_paste, endpoint, text)

    async def report(self):
        pointers = []
        for endpoint in sorted(self.pointer_endpoints):
            try:
                async with asyncio.timeout(1):
                    async with clipboard.CdpClient(endpoint) as cdp:
                        pointers.append({'endpoint': endpoint, 'state': await cdp.call(
                            'Runtime.evaluate', expression=_POINTER_REPORT, awaitPromise=True, returnByValue=True)})
            except Exception as exc:
                pointers.append({'endpoint': endpoint, 'diagnostic_error': repr(exc)})
        pages = []
        for endpoint in sorted(self.endpoints):
            try:
                pages.append(await _page_state(endpoint))
            except Exception as exc:
                pages.append({"endpoint": endpoint, "diagnostic_error": repr(exc)})
        print("Native clipboard failure diagnostics:", json.dumps({
            "trace": self.events, "pointer": pointers, "pointer_calls": self.pointer_calls, "after_failure": pages,
        }, indent=2))


_POINTER_INSTALL = """(() => {
  const points = POINTS;
  let state = window.__corsiaPointerDiagnostic;
  if (!state) {
    state = window.__corsiaPointerDiagnostic = {drags: [], current: null};
    for (const type of ['pointerdown', 'pointermove', 'pointerup', 'pointercancel',
                        'mousedown', 'mousemove', 'mouseup', 'click']) {
      document.addEventListener(type, event => {
        const drag = state.current;
        if (drag && drag.events.length < 100)
          drag.events.push({type: event.type, clientX: event.clientX, clientY: event.clientY});
      }, {capture: true, passive: true});
    }
  }
  const hit = point => {
    const element = document.elementFromPoint(point[0], point[1]);
    return element ? {tag: element.tagName, id: element.id || null} : null;
  };
  const drag = {start: points.start, end: points.end,
    startHit: hit(points.start), endHit: hit(points.end),
    innerWidth, innerHeight, visibilityState: document.visibilityState,
    hasFocus: document.hasFocus(), events: [], rafWithin200ms: null};
  state.drags.push(drag);
  if (state.drags.length > 20) state.drags.shift();
  state.current = drag;
})()"""

_POINTER_RAF = """(() => {
  const drag = window.__corsiaPointerDiagnostic?.current;
  if (!drag) return;
  const started = performance.now();
  const timer = setTimeout(() => { if (drag.rafWithin200ms === null) drag.rafWithin200ms = false; }, 200);
  requestAnimationFrame(() => {
    if (drag.rafWithin200ms === null) {
      drag.rafWithin200ms = performance.now() - started <= 200;
      clearTimeout(timer);
    }
  });
})()"""

_POINTER_REPORT = """new Promise(resolve => {
  const state = window.__corsiaPointerDiagnostic;
  if (!state || !state.drags.some(drag => drag.rafWithin200ms === null)) { resolve(state || null); return; }
  setTimeout(() => resolve(state), 210);
})"""


_FOCUS_STATE = """(() => {
  const chain = []; let doc = document; let selection = ''; let inaccessible = false;
  while (doc) {
    let node = doc.activeElement;
    const active = [];
    while (node) {
      active.push({tag: node.tagName, id: node.id || null});
      if (!node.shadowRoot?.activeElement) break;
      node = node.shadowRoot.activeElement;
    }
    let selected = doc.getSelection()?.toString() || '';
    if (node && (node.tagName === 'INPUT' || node.tagName === 'TEXTAREA')) {
      selected = node.type === 'password' ? '[password omitted]' :
        (node.selectionStart == null ? '' : node.value.slice(node.selectionStart, node.selectionEnd));
    }
    chain.push({active, hasFocus: doc.hasFocus(), visibilityState: doc.visibilityState,
      copyEnabled: doc.queryCommandEnabled('copy'), selected});
    selection = selected;
    if (!node || !['IFRAME', 'FRAME'].includes(node.tagName)) break;
    try { doc = node.contentDocument; if (!doc) inaccessible = true; }
    catch (_) { inaccessible = true; doc = null; }
  }
  return {chain, selection: inaccessible ? null : selection, inaccessibleFrame: inaccessible,
    fixtureEvents: window.clipboardEvents || null};
})()"""


async def _x_command(executable, *args, environment=None):
    process = await asyncio.create_subprocess_exec(executable, *args, env=environment, stdout=asyncio.subprocess.PIPE,
                                                   stderr=asyncio.subprocess.DEVNULL)
    try:
        output, _ = await asyncio.wait_for(process.communicate(), 0.3)
        return {'exit': process.returncode, 'output': output[:8192].decode(errors='replace').strip()}
    finally:
        # Also clean up when the enclosing diagnostic budget cancels communicate().
        if process.returncode is None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            await process.wait()


def _x_environment(socket_dir):
    # The client strips the fixture profile; identify its Chrome by the socket
    # directory inherited from its daemon and use Chrome's actual profile.
    if not socket_dir:
        return None
    for process in psutil.process_iter(['cmdline']):
        try:
            args = process.info['cmdline'] or []
            profile = next((arg.partition('=')[2] for arg in args
                            if arg.startswith('--user-data-dir=') and arg.partition('=')[2]), None)
            if not profile:
                continue
            values = process.environ()
            if values.get('AGENT_BROWSER_SOCKET_DIR') != str(socket_dir) or not values.get('DISPLAY'):
                continue
            environment = dict(os.environ, DISPLAY=values['DISPLAY'])
            environment.pop('XAUTHORITY', None)
            if values.get('XAUTHORITY'):
                environment['XAUTHORITY'] = values['XAUTHORITY']
            return environment, profile
        except (psutil.Error, OSError):
            continue
    return None


async def _x_focus(result, socket_dir):
    owned = _x_environment(socket_dir)
    if owned is None:
        result['unavailable'] = 'no DISPLAY for this fixture browser'
        return result
    environment, _ = owned
    result['display'] = environment['DISPLAY']
    xdotool, xprop, xwininfo = (shutil.which(name) for name in ('xdotool', 'xprop', 'xwininfo'))
    for name, executable, args in (('input_focus', xdotool, ('getwindowfocus',)),
                                   ('active_window_property', xprop, ('-root', '_NET_ACTIVE_WINDOW'))):
        if not executable:
            result[name] = 'unavailable: tool not installed'
            continue
        try:
            result[name] = await _x_command(executable, *args, environment=environment)
        except Exception as exc:
            result[name] = {'diagnostic_error': repr(exc)}
    focus = result.get('input_focus')
    window = focus.get('output') if isinstance(focus, dict) and focus.get('exit') == 0 else None
    if not xwininfo:
        result['window_map'] = 'unavailable: xwininfo not installed'
        return result
    try:
        if window and window.isdecimal():
            windows = [window]
            result['map_source'] = 'input focus window'
        else:
            tree = await _x_command(xwininfo, '-root', '-tree', environment=environment)
            result['root_tree'] = tree
            windows = list(dict.fromkeys(re.findall(r'^\s+(0x[0-9a-fA-F]+)\s', tree['output'], re.MULTILINE)))[:4]
            result['map_source'] = 'first four root-tree windows; input focus unavailable'
        result['window_map'] = []
        for window_id in windows:
            info = await _x_command(xwininfo, '-id', window_id, environment=environment)
            lines = [line.strip() for line in info['output'].splitlines() if 'Map State:' in line]
            result['window_map'].append({'window': window_id, 'map_state': lines, 'exit': info['exit']})
    except Exception as exc:
        result['window_map_error'] = repr(exc)
    return result


_CHILD_FOCUS_STATE = """new Promise(resolve => {
  const frame = document.activeElement;
  if (!frame || !['IFRAME', 'FRAME'].includes(frame.tagName) || !frame.contentWindow) {
    resolve({unavailable: 'no active frame'}); return;
  }
  const child = frame.contentWindow;
  const nonce = Math.random().toString(36);
  const finish = result => { clearTimeout(timer); window.removeEventListener('message', receive); resolve(result); };
  const receive = event => {
    if (event.source === child && event.data?.type === 'clipboard-diagnostic-response' && event.data.nonce === nonce)
      finish(event.data);
  };
  const timer = setTimeout(() => finish({unavailable: 'child response timed out'}), 200);
  window.addEventListener('message', receive);
  child.postMessage({type: 'clipboard-diagnostic-request', nonce}, '*');
})"""


async def _before_key(cdp, launch_time, socket_dir):
    state = {'seconds_since_launch': round(time.monotonic() - launch_time, 6) if launch_time else None,
             'focused_frame_id': None, 'focused_frame_note': 'not exposed by these probes'}
    try:
        async with asyncio.timeout(1):
            # Call the client directly, not the monkeypatched clipboard._call.
            for key, method, params in (
                ('target', 'Target.getTargetInfo', {}),
                ('frames', 'Page.getFrameTree', {}),
                ('page', 'Runtime.evaluate', {'expression': _FOCUS_STATE, 'returnByValue': True}),
            ):
                try:
                    state[key] = await cdp.call(method, **params)
                except Exception as exc:
                    state[key] = {'diagnostic_error': repr(exc)}
            page = state.get('page', {}).get('result', {}).get('value', {})
            if isinstance(page, dict) and page.get('inaccessibleFrame'):
                try:
                    state['child_at_pre_key'] = await cdp.call(
                        'Runtime.evaluate', expression=_CHILD_FOCUS_STATE, awaitPromise=True, returnByValue=True)
                except Exception as exc:
                    state['child_at_pre_key'] = {'diagnostic_error': repr(exc)}
            state['x_focus'] = {}
            await _x_focus(state['x_focus'], socket_dir)
    except Exception as exc:
        state['diagnostic_error'] = repr(exc)
    return state


async def _page_state(endpoint):
    async with clipboard.CdpClient(endpoint) as cdp:
        context, url = await clipboard._world(cdp)
        isolated = await clipboard._evaluate(cdp, context, """({
            installed: window.__corsiaClipboardListener,
            sequence: window.__corsiaClipboardSequence,
            record: window.__corsiaClipboardRecord,
            focus: document.hasFocus(), visibility: document.visibilityState
        })""")
        page = await clipboard._call(cdp, "Runtime.evaluate", "diagnostic page state",
            expression="({focus:document.hasFocus(),visibility:document.visibilityState,events:window.clipboardEvents})",
            returnByValue=True)
        return {"endpoint": endpoint, "url": url, "isolated": isolated, "page": page}


async def _install_pointer(trace, native_owner, thread, start, end):
    try:
        async with asyncio.timeout(1):
            endpoint = await native_owner.page_endpoint(thread)
            trace.pointer_endpoints.add(endpoint)
            async with clipboard.CdpClient(endpoint) as cdp:
                expression = _POINTER_INSTALL.replace('POINTS', json.dumps({'start': start, 'end': end}))
                await cdp.call('Runtime.evaluate', expression=expression, returnByValue=True)
            return endpoint
    except Exception as exc:
        trace.events.append({'pointer_diagnostic_error': repr(exc)})
        return None


async def _start_pointer_raf(trace, endpoint):
    if not endpoint:
        return
    try:
        async with asyncio.timeout(1):
            async with clipboard.CdpClient(endpoint) as cdp:
                await cdp.call('Runtime.evaluate', expression=_POINTER_RAF, returnByValue=True)
    except Exception as exc:
        trace.events.append({'pointer_raf_error': repr(exc)})


@pytest.fixture
async def native_clipboard_diagnostics(native_owner, monkeypatch, request):
    """Expose hidden helper errors only when a synthetic clipboard test fails."""
    trace = _ClipboardTrace(native_owner.socket_dir)
    original_run = native_owner.run

    async def run_with_launch_time(*args, **kwargs):
        command = args[1] if len(args) > 1 else kwargs.get('args', ())
        if trace.launch_time is None and command and command[0] == 'open':
            trace.launch_time = time.monotonic()
        return await original_run(*args, **kwargs)

    monkeypatch.setattr(native_owner, 'run', run_with_launch_time)
    original_drag = request.module._drag_selection
    original_mouse = request.module.browser_pane.mouse_event

    async def mouse_with_diagnostics(thread, event, connection):
        entry = {'event': dict(event)}
        trace.pointer_calls.append(entry)
        if len(trace.pointer_calls) > 60:
            trace.pointer_calls.pop(0)
        try:
            result = await original_mouse(thread, event, connection)
            entry['result'] = repr(result)
            return result
        except Exception as exc:
            entry['error'] = repr(exc)
            raise

    async def drag_with_diagnostics(thread, connection, start, end):
        endpoint = await _install_pointer(trace, native_owner, thread, start, end)
        try:
            return await original_drag(thread, connection, start, end)
        finally:
            await _start_pointer_raf(trace, endpoint)

    monkeypatch.setattr(request.module.browser_pane, 'mouse_event', mouse_with_diagnostics)
    monkeypatch.setattr(request.module, '_drag_selection', drag_with_diagnostics)
    failed_before = request.session.testsfailed
    logger = logging.getLogger("host_access.browser_cdp")
    previous_level = logger.level
    logger.addHandler(trace)
    logger.setLevel(logging.DEBUG)
    monkeypatch.setattr(clipboard, "_call", trace.call)
    monkeypatch.setattr(clipboard, "clipboard_selection", trace.selection)
    monkeypatch.setattr(clipboard, "clipboard_paste", trace.paste)
    try:
        yield
    finally:
        try:
            if request.session.testsfailed > failed_before:
                # native_owner still owns these fixture pages. This is state after
                # failure, not a claim about focus at the instant of key dispatch.
                await trace.report()
        finally:
            logger.removeHandler(trace)
            logger.setLevel(previous_level)
