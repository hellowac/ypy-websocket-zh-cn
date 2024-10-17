from __future__ import annotations

from contextlib import AsyncExitStack
from functools import partial
from inspect import isawaitable
from logging import Logger, getLogger
from typing import Awaitable, Callable

import y_py as Y
from anyio import (
    TASK_STATUS_IGNORED,
    Event,
    create_memory_object_stream,
    create_task_group,
)
from anyio.abc import TaskGroup, TaskStatus
from anyio.streams.memory import MemoryObjectReceiveStream, MemoryObjectSendStream

from .awareness import Awareness
from .websocket import Websocket
from .ystore import BaseYStore
from .yutils import (
    YMessageType,
    create_update_message,
    process_sync_message,
    put_updates,
    sync,
)


class YRoom:
    clients: list
    ydoc: Y.YDoc
    ystore: BaseYStore | None
    _on_message: Callable[[bytes], Awaitable[bool] | bool] | None
    _update_send_stream: MemoryObjectSendStream
    _update_receive_stream: MemoryObjectReceiveStream
    _ready: bool
    _task_group: TaskGroup | None
    _started: Event | None
    _starting: bool

    def __init__(
        self,
        ready: bool = True,
        ystore: BaseYStore | None = None,
        log: Logger | None = None,
    ):
        """初始化对象。

        YRoom 实例应优先作为异步上下文管理器使用：

        ```py
        async with room:
            ...
        ```

        但是，也可以使用更低级的 API：

        ```py
        task = asyncio.create_task(room.start())
        await room.started.wait()
        ...
        room.stop()
        ```

        Arguments:
            ready: 内部 YDoc 是否准备好立即进行同步。
            ystore: 可选的存储，用于持久化文档更新。
            log: 可选的日志记录器。
        """
        self.ydoc = Y.YDoc()
        self.awareness = Awareness(self.ydoc)
        self._update_send_stream, self._update_receive_stream = (
            create_memory_object_stream(max_buffer_size=65536)
        )
        self._ready = False
        self.ready = ready
        self.ystore = ystore
        self.log = log or getLogger(__name__)
        self.clients = []
        self._on_message = None
        self._started = None
        self._starting = False
        self._task_group = None

    @property
    def started(self):
        """YRoom 提供程序启动时设置的异步事件。"""
        if self._started is None:
            self._started = Event()
        return self._started

    @property
    def ready(self) -> bool:
        """
        Returns:
            True 表示内部 YDoc 已准备好同步。
        """
        return self._ready

    @ready.setter
    def ready(self, value: bool) -> None:
        """
        Arguments:
            value: 如果内部 YDoc 已准备好同步，则为 True，否则为 False。"""
        self._ready = value
        if value:
            self.ydoc.observe_after_transaction(
                partial(put_updates, self._update_send_stream)
            )

    @property
    def on_message(self) -> Callable[[bytes], Awaitable[bool] | bool] | None:
        """
        Returns:
            收到消息时调用的可选回调。
        """
        return self._on_message

    @on_message.setter
    def on_message(self, value: Callable[[bytes], Awaitable[bool] | bool] | None):
        """
        Arguments:
            value: 收到消息时调用的可选回调。如果回调返回 True，则跳过该消息。
        """
        self._on_message = value

    async def _broadcast_updates(self):
        if self.ystore is not None and not self.ystore.started.is_set():
            self._task_group.start_soon(self.ystore.start)

        async with self._update_receive_stream:
            async for update in self._update_receive_stream:
                if self._task_group.cancel_scope.cancel_called:
                    return
                # 将内部 ydoc 的更新广播到所有客户端，包括来自客户端的更改和来自后端的更改（带外更改）
                for client in self.clients:
                    self.log.debug(
                        "Sending Y update to client with endpoint: %s", client.path
                    )
                    message = create_update_message(update)
                    self._task_group.start_soon(client.send, message)
                if self.ystore:
                    self.log.debug("Writing Y update to YStore")
                    self._task_group.start_soon(self.ystore.write, update)

    async def __aenter__(self) -> YRoom:
        if self._task_group is not None:
            raise RuntimeError("YRoom already running")

        async with AsyncExitStack() as exit_stack:
            tg = create_task_group()
            self._task_group = await exit_stack.enter_async_context(tg)
            self._exit_stack = exit_stack.pop_all()
            tg.start_soon(self._broadcast_updates)
            self.started.set()

        return self

    async def __aexit__(self, exc_type, exc_value, exc_tb):
        if self._task_group is None:
            raise RuntimeError("YRoom not running")

        self._task_group.cancel_scope.cancel()
        self._task_group = None
        return await self._exit_stack.__aexit__(exc_type, exc_value, exc_tb)

    async def start(self, *, task_status: TaskStatus[None] = TASK_STATUS_IGNORED):
        """启动room

        Arguments:
            task_status: 任务开始时设置的状态。
        """
        if self._starting:
            return
        else:
            self._starting = True

        if self._task_group is not None:
            raise RuntimeError("YRoom already running")

        async with create_task_group() as self._task_group:
            self._task_group.start_soon(self._broadcast_updates)
            self.started.set()
            self._starting = False
            task_status.started()

    def stop(self):
        """停止room."""
        if self._task_group is None:
            raise RuntimeError("YRoom not running")

        self._task_group.cancel_scope.cancel()
        self._task_group = None

    async def serve(self, websocket: Websocket):
        """服务一个客户端

        Arguments:
            websocket: 用于为客户端提供服务的 WebSocket。
        """
        async with create_task_group() as tg:
            self.clients.append(websocket)
            await sync(self.ydoc, websocket, self.log)
            try:
                async for message in websocket:
                    # 过滤消息 (e.g. awareness)
                    skip = False
                    if self.on_message:
                        _skip = self.on_message(message)
                        skip = await _skip if isawaitable(_skip) else _skip
                    if skip:
                        continue
                    message_type = message[0]
                    if message_type == YMessageType.SYNC:
                        # 在后台更新我们的内部状态，对内部状态的更改随后将转发给所有客户端并存储在 YStore 中（如果有）
                        tg.start_soon(
                            process_sync_message,
                            message[1:],
                            self.ydoc,
                            websocket,
                            self.log,
                        )
                    elif message_type == YMessageType.AWARENESS:
                        # 将感知消息从此客户端转发给所有客户端，包括它自己，因为它用于保持连接处于活动状态
                        self.log.debug(
                            "Received %s message from endpoint: %s",
                            YMessageType.AWARENESS.name,
                            websocket.path,
                        )
                        for client in self.clients:
                            self.log.debug(
                                "Sending Y awareness from client with endpoint %s to client with endpoint: %s",
                                websocket.path,
                                client.path,
                            )
                            tg.start_soon(client.send, message)
            except Exception as e:
                self.log.debug("Error serving endpoint: %s", websocket.path, exc_info=e)

            # remove this client
            # 移除这个客户端
            self.clients = [c for c in self.clients if c != websocket]
