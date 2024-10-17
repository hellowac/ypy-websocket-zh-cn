from __future__ import annotations

from contextlib import AsyncExitStack
from logging import Logger, getLogger

from anyio import TASK_STATUS_IGNORED, Event, create_task_group
from anyio.abc import TaskGroup, TaskStatus

from .websocket import Websocket
from .yroom import YRoom


class WebsocketServer:
    """WebSocket server."""

    auto_clean_rooms: bool
    rooms: dict[str, YRoom]
    _started: Event | None
    _starting: bool
    _task_group: TaskGroup | None

    def __init__(
        self,
        rooms_ready: bool = True,
        auto_clean_rooms: bool = True,
        log: Logger | None = None,
    ) -> None:
        """初始化对象。

        WebsocketServer 实例最好作为异步上下文管理器使用：

        ```py
        async with websocket_server:
            ...
        ```

        不过，也可以使用更低级的 API：

        ```py
        task = asyncio.create_task(websocket_server.start())
        await websocket_server.started.wait()
        ...
        websocket_server.stop()
        ```

        Arguments:
            rooms_ready: 打开时房间是否准备好进行同步。
            auto_clean_rooms: 当没有客户端时，房间是否应该被删除。
            log: 可选的日志记录器。
        """
        self.rooms_ready = rooms_ready
        self.auto_clean_rooms = auto_clean_rooms
        self.log = log or getLogger(__name__)
        self.rooms = {}
        self._started = None
        self._starting = False
        self._task_group = None

    @property
    def started(self) -> Event:
        """WebSocket 服务器启动时设置的异步事件。"""
        if self._started is None:
            self._started = Event()
        return self._started

    async def get_room(self, name: str) -> YRoom:
        """获取或创建一个具有给定名称的房间，并启动它。

        Arguments:
            name: 房间名称

        Returns:
            具有给定名称的房间，如果未找到具有该名称的房间，则为新房间。
        """
        if name not in self.rooms.keys():
            self.rooms[name] = YRoom(ready=self.rooms_ready, log=self.log)
        room = self.rooms[name]
        await self.start_room(room)
        return room

    async def start_room(self, room: YRoom) -> None:
        """如果尚未启动，启动一个房间。

        Arguments:
            room: 要启动的房间
        """
        if self._task_group is None:
            raise RuntimeError(
                "The WebsocketServer is not running: use `async with websocket_server:` or `await websocket_server.start()`"
            )

        if not room.started.is_set():
            await self._task_group.start(room.start)

    def get_room_name(self, room: YRoom) -> str:
        """获取房间的名称。

        Arguments:
            room: 获取名字的房间。

        Returns:
            房间名称
        """
        return list(self.rooms.keys())[list(self.rooms.values()).index(room)]

    def rename_room(
        self,
        to_name: str,
        *,
        from_name: str | None = None,
        from_room: YRoom | None = None,
    ) -> None:
        """重命名房间

        Arguments:
            to_name: 房间的新名称
            from_name: 房间的上一个名称 (如果 `from_room` 没有传入).
            from_room: 要重命名的房间 (如果 `from_name` 没有传入).
        """
        if from_name is not None and from_room is not None:
            raise RuntimeError("Cannot pass from_name and from_room")
        if from_name is None:
            assert from_room is not None
            from_name = self.get_room_name(from_room)
        self.rooms[to_name] = self.rooms.pop(from_name)

    def delete_room(
        self, *, name: str | None = None, room: YRoom | None = None
    ) -> None:
        """删除一个房间

        Arguments:
            name: 要删除房间的名称 (如果 `room` 没有传入).
            room: 要删除的房间 ( 如果 `name` 没有传入).
        """
        if name is not None and room is not None:
            raise RuntimeError("Cannot pass name and room")
        if name is None:
            assert room is not None
            name = self.get_room_name(room)
        room = self.rooms.pop(name)
        room.stop()

    async def serve(self, websocket: Websocket) -> None:
        """通过 WebSocket 为客户端提供服务。

        Arguments:
            websocket: 用于为客户端提供服务的 WebSocket。
        """
        if self._task_group is None:
            raise RuntimeError(
                "The WebsocketServer is not running: use `async with websocket_server:` or `await websocket_server.start()`"
            )

        async with create_task_group() as tg:
            tg.start_soon(self._serve, websocket, tg)

    async def _serve(self, websocket: Websocket, tg: TaskGroup):
        room = await self.get_room(websocket.path)
        await self.start_room(room)
        await room.serve(websocket)

        if self.auto_clean_rooms and not room.clients:
            self.delete_room(room=room)
        tg.cancel_scope.cancel()

    async def __aenter__(self) -> WebsocketServer:
        if self._task_group is not None:
            raise RuntimeError("WebsocketServer already running")

        async with AsyncExitStack() as exit_stack:
            tg = create_task_group()
            self._task_group = await exit_stack.enter_async_context(tg)
            self._exit_stack = exit_stack.pop_all()
            self.started.set()

        return self

    async def __aexit__(self, exc_type, exc_value, exc_tb):
        if self._task_group is None:
            raise RuntimeError("WebsocketServer not running")

        self._task_group.cancel_scope.cancel()
        self._task_group = None
        return await self._exit_stack.__aexit__(exc_type, exc_value, exc_tb)

    async def start(self, *, task_status: TaskStatus[None] = TASK_STATUS_IGNORED):
        """启动 WebSocket 服务。

        Arguments:
            task_status: 任务开始时设置的状态。
        """
        if self._starting:
            return
        else:
            self._starting = True

        if self._task_group is not None:
            raise RuntimeError("WebsocketServer already running")

        # 创建任务组并等待
        async with create_task_group() as self._task_group:
            self._task_group.start_soon(Event().wait)
            self.started.set()
            self._starting = False
            task_status.started()

    def stop(self) -> None:
        """停止 WebSocket 服务."""
        if self._task_group is None:
            raise RuntimeError("WebsocketServer not running")

        self._task_group.cancel_scope.cancel()
        self._task_group = None
