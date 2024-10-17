from __future__ import annotations

from contextlib import AsyncExitStack
from functools import partial
from logging import Logger, getLogger

import y_py as Y
from anyio import (
    TASK_STATUS_IGNORED,
    Event,
    create_memory_object_stream,
    create_task_group,
)
from anyio.abc import TaskGroup, TaskStatus
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from .websocket import Websocket
from .yutils import (
    YMessageType,
    create_update_message,
    process_sync_message,
    put_updates,
    sync,
)


class WebsocketProvider:
    """WebSocket provider."""

    _ydoc: Y.YDoc
    _update_send_stream: MemoryObjectSendStream
    _update_receive_stream: MemoryObjectReceiveStream
    _started: Event | None
    _starting: bool
    _task_group: TaskGroup | None

    def __init__(
        self, ydoc: Y.YDoc, websocket: Websocket, log: Logger | None = None
    ) -> None:
        """初始化对象

        WebsocketProvider 实例最好作为异步上下文管理器使用：

        ```py
        async with websocket_provider:
            ...
        ```

        不过，也可以使用更低级的 API：

        ```py
        task = asyncio.create_task(websocket_provider.start())
        await websocket_provider.started.wait()
        ...
        websocket_provider.stop()
        ```

        Arguments:
            ydoc: 通过 WebSocket 连接的 YDoc。
            websocket: 通过该 WebSocket 连接 YDoc。
            log: 可选的日志记录器。
        """
        self._ydoc = ydoc
        self._websocket = websocket
        self.log = log or getLogger(__name__)
        self._update_send_stream, self._update_receive_stream = (
            create_memory_object_stream(max_buffer_size=65536)
        )
        self._started = None
        self._starting = False
        self._task_group = None
        ydoc.observe_after_transaction(partial(put_updates, self._update_send_stream))

    @property
    def started(self) -> Event:
        """WebSocket 提供程序启动时设置的异步事件。"""
        if self._started is None:
            self._started = Event()
        return self._started

    async def __aenter__(self) -> WebsocketProvider:
        if self._task_group is not None:
            raise RuntimeError("WebsocketProvider already running")

        async with AsyncExitStack() as exit_stack:
            tg = create_task_group()
            self._task_group = await exit_stack.enter_async_context(tg)
            self._exit_stack = exit_stack.pop_all()
            tg.start_soon(self._run)
            self.started.set()

        return self

    async def __aexit__(self, exc_type, exc_value, exc_tb):
        if self._task_group is None:
            raise RuntimeError("WebsocketProvider not running")

        self._task_group.cancel_scope.cancel()
        self._task_group = None
        return await self._exit_stack.__aexit__(exc_type, exc_value, exc_tb)

    async def _run(self):
        await sync(self._ydoc, self._websocket, self.log)
        self._task_group.start_soon(self._send)
        async for message in self._websocket:
            if message[0] == YMessageType.SYNC:
                await process_sync_message(
                    message[1:], self._ydoc, self._websocket, self.log
                )

    async def _send(self):
        async with self._update_receive_stream:
            async for update in self._update_receive_stream:
                message = create_update_message(update)
                try:
                    await self._websocket.send(message)
                except Exception:
                    pass

    async def start(self, *, task_status: TaskStatus[None] = TASK_STATUS_IGNORED):
        """启动 WebSocket 提供程序。

        Arguments:
            task_status: 任务开始时设置的状态。
        """
        if self._starting:
            return
        else:
            self._starting = True

        if self._task_group is not None:
            raise RuntimeError("WebsocketProvider already running")

        async with create_task_group() as self._task_group:
            self._task_group.start_soon(self._run)
            self.started.set()
            self._starting = False
            task_status.started()

    def stop(self):
        """停止 WebSocket 提供程序。"""
        if self._task_group is None:
            raise RuntimeError("WebsocketProvider not running")

        self._task_group.cancel_scope.cancel()
        self._task_group = None
