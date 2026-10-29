"""The headed clipboard fixture starts input only after its own window is shown."""
import asyncio
import json
import re
import shutil

from host_access import browser_screencast
from tests.browser_clipboard_diagnostics import _x_command, _x_environment


async def wait_for_headed_window(endpoint, socket_dir):
    # A real pane cannot be pressed until it shows a frame. In the native test,
    # open() can finish while X is still mapping Chrome; that map sends a native
    # buttonless mouse move which can interrupt an already-started CDP drag.
    executable = shutil.which('xwininfo')
    assert executable, 'headed fixture requires xwininfo'
    owned = None
    try:
        async with asyncio.timeout(10):
            while True:
                owned = await asyncio.to_thread(_x_environment, socket_dir)
                if owned:
                    environment, profile = owned
                    try:
                        tree = await _x_command(executable, '-root', '-tree', environment=environment)
                        windows = [re.match(r'\s*(0x[0-9a-fA-F]+)', line).group(1)
                                   for line in tree['output'].splitlines()
                                   if profile in line and re.match(r'\s*(0x[0-9a-fA-F]+)', line)]
                        if windows:
                            info = await _x_command(executable, '-id', windows[0], environment=environment)
                            if info['exit'] == 0 and 'Map State: IsViewable' in info['output']:
                                break
                    except TimeoutError:
                        pass  # A single X probe timed out; the overall budget still applies.
                await asyncio.sleep(0.02)
            # Consume an actual rendered frame after the map, as the pane does.
            frames = asyncio.Queue()
            cast = browser_screencast.PaneScreencast(endpoint, lambda message: frames.put_nowait(json.loads(message)))
            running = asyncio.create_task(cast.run())
            try:
                frame = await frames.get()
                assert frame['data']
            finally:
                running.cancel()
                await asyncio.gather(running, return_exceptions=True)
    except TimeoutError as exc:
        if owned is None:
            raise AssertionError('headed fixture has no owned X display') from exc
        raise AssertionError('headed fixture window or frame did not become ready') from exc
