"""asyncio helpers."""
import asyncio
import functools


async def run_in_thread(func, *args, **kwargs):
    """Run blocking ``func(*args, **kwargs)`` in the default thread executor.

    A back-port of :func:`asyncio.to_thread` (added in Python 3.9) so nsh keeps
    working on Python 3.8. Must be awaited from within a running event loop.
    """
    loop = asyncio.get_running_loop()
    return await loop.run_in_executor(None, functools.partial(func, *args, **kwargs))
