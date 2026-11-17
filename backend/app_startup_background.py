"""Own finite startup background work through shutdown."""

import asyncio


async def run_startup_background(drive) -> None:
    """Own and join recovery alongside the existing finite startup drive."""
    from plans import public as plans

    tasks = [asyncio.create_task(drive), asyncio.create_task(plans.recover_person_comments())]
    try:
        results = await asyncio.gather(*tasks, return_exceptions=True)
        for result in results:
            if isinstance(result, BaseException):
                raise result
    finally:
        for task in tasks:
            if not task.done():
                task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
