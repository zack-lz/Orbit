"""Keep all Windows media/COM calls on one dedicated thread and event loop."""
import asyncio
import threading


class PlayerWorker:
    def __init__(self, player):
        self.player = player
        self.thread = None
        self.loop = None
        self.on_change = None

    async def start(self):
        if self.thread is None:
            caller = asyncio.get_running_loop()
            ready = caller.create_future()

            def signal_ready(error=None):
                if not ready.done():
                    if error is None:
                        ready.set_result(None)
                    else:
                        ready.set_exception(error)

            def run():
                try:
                    self.loop = asyncio.new_event_loop()
                    asyncio.set_event_loop(self.loop)
                except Exception as error:
                    caller.call_soon_threadsafe(signal_ready, error)
                    return
                caller.call_soon_threadsafe(signal_ready)
                try:
                    self.loop.run_forever()
                finally:
                    self.loop.close()

            self.thread = threading.Thread(target=run, name='WindowsMedia', daemon=True)
            self.thread.start()
            await ready
        caller = asyncio.get_running_loop()
        self.player.on_change = lambda: caller.call_soon_threadsafe(self.on_change) if self.on_change else None
        await self.call(self.player.start())

    async def call(self, coroutine):
        return await asyncio.wrap_future(asyncio.run_coroutine_threadsafe(coroutine, self.loop))

    async def snapshot(self):
        return await self.call(self.player.snapshot())

    async def command(self, action, value=None):
        return await self.call(self.player.command(action, value))

    @property
    def art(self):
        return getattr(self.player, 'art', b'')

    @property
    def art_type(self):
        return getattr(self.player, 'art_type', 'image/jpeg')

    @property
    def art_version(self):
        return getattr(self.player, 'art_version', '')

    async def close(self):
        if self.loop is None or not self.loop.is_running():
            return

        async def cancel_pending():
            if hasattr(self.player, 'close'):
                await self.player.close()
            tasks = [task for task in asyncio.all_tasks() if task is not asyncio.current_task()]
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)

        try:
            await asyncio.wait_for(self.call(cancel_pending()), timeout=2)
        except asyncio.TimeoutError:
            pass
        self.loop.call_soon_threadsafe(self.loop.stop)
        await asyncio.to_thread(self.thread.join, 2)
