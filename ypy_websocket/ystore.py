from __future__ import annotations

import struct
import tempfile
import time
from abc import ABC, abstractmethod
from contextlib import AsyncExitStack
from inspect import isawaitable
from logging import Logger, getLogger
from pathlib import Path
from typing import AsyncIterator, Awaitable, Callable, cast

import aiosqlite
import anyio
import y_py as Y
from anyio import TASK_STATUS_IGNORED, Event, Lock, create_task_group
from anyio.abc import TaskGroup, TaskStatus

from .yutils import Decoder, get_new_path, write_var_uint


class YDocNotFound(Exception):
    pass


class BaseYStore(ABC):
    metadata_callback: Callable[[], Awaitable[bytes] | bytes] | None = None
    version = 2
    _started: Event | None = None
    _starting: bool = False
    _task_group: TaskGroup | None = None

    @abstractmethod
    def __init__(
        self,
        path: str,
        metadata_callback: Callable[[], Awaitable[bytes] | bytes] | None = None,
    ): ...

    @abstractmethod
    async def write(self, data: bytes) -> None: ...

    @abstractmethod
    async def read(self) -> AsyncIterator[tuple[bytes, bytes]]: ...

    @property
    def started(self) -> Event:
        if self._started is None:
            self._started = Event()
        return self._started

    async def __aenter__(self) -> BaseYStore:
        if self._task_group is not None:
            raise RuntimeError("YStore already running")

        async with AsyncExitStack() as exit_stack:
            tg = create_task_group()
            self._task_group = await exit_stack.enter_async_context(tg)
            self._exit_stack = exit_stack.pop_all()
            tg.start_soon(self.start)

        return self

    async def __aexit__(self, exc_type, exc_value, exc_tb):
        if self._task_group is None:
            raise RuntimeError("YStore not running")

        self._task_group.cancel_scope.cancel()
        self._task_group = None
        return await self._exit_stack.__aexit__(exc_type, exc_value, exc_tb)

    async def start(self, *, task_status: TaskStatus[None] = TASK_STATUS_IGNORED):
        """启动 store.

        Arguments:
            task_status: 任务开始时设置的状态。
        """
        if self._starting:
            return
        else:
            self._starting = True

        if self._task_group is not None:
            raise RuntimeError("YStore already running")

        self.started.set()
        self._starting = False
        task_status.started()

    def stop(self) -> None:
        """停止 store."""
        if self._task_group is None:
            raise RuntimeError("YStore not running")

        self._task_group.cancel_scope.cancel()
        self._task_group = None

    async def get_metadata(self) -> bytes:
        """
        Returns:
            元数据(metadata).
        """
        if self.metadata_callback is None:
            return b""

        metadata = self.metadata_callback()
        if isawaitable(metadata):
            metadata = await metadata
        metadata = cast(bytes, metadata)
        return metadata

    async def encode_state_as_update(self, ydoc: Y.YDoc) -> None:
        """存储1个 YDoc 状态.

        Arguments:
            ydoc: 用于存储状态的 YDoc。
        """
        update = Y.encode_state_as_update(ydoc)  # type: ignore
        await self.write(update)

    async def apply_updates(self, ydoc: Y.YDoc) -> None:
        """将所有存储的更新应用到 YDoc。

        Arguments:
            ydoc: 要应用更新的 YDoc。
        """
        async for update, *rest in self.read():  # type: ignore
            Y.apply_update(ydoc, update)  # type: ignore


class FileYStore(BaseYStore):
    """每个文档使用一个文件的 YStore。"""

    path: str
    metadata_callback: Callable[[], Awaitable[bytes] | bytes] | None
    lock: Lock

    def __init__(
        self,
        path: str,
        metadata_callback: Callable[[], Awaitable[bytes] | bytes] | None = None,
        log: Logger | None = None,
    ) -> None:
        """初始化对象

        Arguments:
            path: 用于存储更新的文件路径。
            metadata_callback: 调用以获取元数据的可选回调。
            log: 可选记录器(logger).
        """
        self.path = path
        self.metadata_callback = metadata_callback
        self.log = log or getLogger(__name__)
        self.lock = Lock()

    async def check_version(self) -> int:
        """检查商店格式的版本。

        Returns:
            数据在文件中的偏移量。
        """
        if not await anyio.Path(self.path).exists():
            version_mismatch = True
        else:
            version_mismatch = False
            move_file = False
            async with await anyio.open_file(self.path, "rb") as f:
                header = await f.read(8)
                if header == b"VERSION:":
                    version = int(await f.readline())
                    if version == self.version:
                        offset = await f.tell()
                    else:
                        version_mismatch = True
                else:
                    version_mismatch = True
                if version_mismatch:
                    move_file = True
            if move_file:
                new_path = await get_new_path(self.path)
                self.log.warning(
                    f"YStore version mismatch, moving {self.path} to {new_path}"
                )
                await anyio.Path(self.path).rename(new_path)
        if version_mismatch:
            async with await anyio.open_file(self.path, "wb") as f:
                version_bytes = f"VERSION:{self.version}\n".encode()
                await f.write(version_bytes)
                offset = len(version_bytes)
        return offset

    async def read(self) -> AsyncIterator[tuple[bytes, bytes, float]]:  # type: ignore
        """用于读取存储内容的异步迭代器。

        Returns:
            每个更新的一个元组， 结构是: (update, metadata, timestamp)
        """
        async with self.lock:
            if not await anyio.Path(self.path).exists():
                raise YDocNotFound
            offset = await self.check_version()
            async with await anyio.open_file(self.path, "rb") as f:
                await f.seek(offset)
                data = await f.read()
                if not data:
                    raise YDocNotFound
        i = 0
        for d in Decoder(data).read_messages():
            if i == 0:
                update = d
            elif i == 1:
                metadata = d
            else:
                timestamp = struct.unpack("<d", d)[0]
                yield update, metadata, timestamp
            i = (i + 1) % 3

    async def write(self, data: bytes) -> None:
        """存储1个更新

        Arguments:
            data: 要存储的更新。
        """
        parent = Path(self.path).parent
        async with self.lock:
            await anyio.Path(parent).mkdir(parents=True, exist_ok=True)
            await self.check_version()
            async with await anyio.open_file(self.path, "ab") as f:
                data_len = write_var_uint(len(data))
                await f.write(data_len + data)
                metadata = await self.get_metadata()
                metadata_len = write_var_uint(len(metadata))
                await f.write(metadata_len + metadata)
                timestamp = struct.pack("<d", time.time())
                timestamp_len = write_var_uint(len(timestamp))
                await f.write(timestamp_len + timestamp)


class TempFileYStore(FileYStore):
    """使用系统临时目录的 YStore。文件写入公共目录下。要为目录名称添加前缀（例如 /tmp/my_prefix_b4whmm7y/）：

    ```py
    class PrefixTempFileYStore(TempFileYStore):
        prefix_dir = "my_prefix_"
    ```
    """

    prefix_dir: str | None = None
    base_dir: str | None = None

    def __init__(
        self,
        path: str,
        metadata_callback: Callable[[], Awaitable[bytes] | bytes] | None = None,
        log: Logger | None = None,
    ):
        """初始化对象

        Arguments:
            path: 用于存储更新的文件路径。
            metadata_callback: 调用以获取元数据的可选回调。
            log: 可选的记录器(logger).
        """
        full_path = str(Path(self.get_base_dir()) / path)
        super().__init__(full_path, metadata_callback=metadata_callback, log=log)

    def get_base_dir(self) -> str:
        """获取写入更新文件的基本目录。

        Returns:
            基目录路径。
        """
        if self.base_dir is None:
            self.make_directory()
        assert self.base_dir is not None
        return self.base_dir

    def make_directory(self):
        """创建写入更新文件的基本目录."""
        type(self).base_dir = tempfile.mkdtemp(prefix=self.prefix_dir)


class SQLiteYStore(BaseYStore):
    """使用 SQLite 数据库的 YStore。
    与基于文件的 YStore 不同，所有文档的 Y 更新都存储在同一个数据库中。

    子类指向您的数据库文件：

    ```py
    class MySQLiteYStore(SQLiteYStore):
        db_path = "path/to/my_ystore.db"
    ```
    """

    db_path: str = "ystore.db"
    # 确定所有文档的“生存时间”，即在清除文档历史记录之前文档的最新更新必须有多新。
    # 默认为永不清除文档历史记录（无）。
    document_ttl: int | None = None
    path: str
    lock: Lock
    db_initialized: Event

    def __init__(
        self,
        path: str,
        metadata_callback: Callable[[], Awaitable[bytes] | bytes] | None = None,
        log: Logger | None = None,
    ) -> None:
        """初始化对象.

        Arguments:
            path: 用于存储更新的文件路径。
            metadata_callback: 调用以获取元数据的可选回调。
            log: 可选的记录器(logger).
        """
        self.path = path
        self.metadata_callback = metadata_callback
        self.log = log or getLogger(__name__)
        self.lock = Lock()
        self.db_initialized = Event()

    async def start(self, *, task_status: TaskStatus[None] = TASK_STATUS_IGNORED):
        """启动 SQLiteYStore.

        Arguments:
            task_status: 任务开始时设置的状态。
        """
        if self._starting:
            return
        else:
            self._starting = True

        if self._task_group is not None:
            raise RuntimeError("YStore already running")

        async with create_task_group() as self._task_group:
            self._task_group.start_soon(self._init_db)
            self.started.set()
            self._starting = False
            task_status.started()

    async def _init_db(self):
        create_db = False
        move_db = False
        if not await anyio.Path(self.db_path).exists():
            create_db = True
        else:
            async with self.lock:
                async with aiosqlite.connect(self.db_path) as db:
                    cursor = await db.execute(
                        "SELECT count(name) FROM sqlite_master WHERE type='table' and name='yupdates'"
                    )
                    table_exists = (await cursor.fetchone())[0]
                    if table_exists:
                        cursor = await db.execute("pragma user_version")
                        version = (await cursor.fetchone())[0]
                        if version != self.version:
                            move_db = True
                            create_db = True
                    else:
                        create_db = True
        if move_db:
            new_path = await get_new_path(self.db_path)
            self.log.warning(
                f"YStore version mismatch, moving {self.db_path} to {new_path}"
            )
            await anyio.Path(self.db_path).rename(new_path)
        if create_db:
            async with self.lock:
                async with aiosqlite.connect(self.db_path) as db:
                    await db.execute(
                        "CREATE TABLE yupdates (path TEXT NOT NULL, yupdate BLOB, metadata BLOB, timestamp REAL NOT NULL)"
                    )
                    await db.execute(
                        "CREATE INDEX idx_yupdates_path_timestamp ON yupdates (path, timestamp)"
                    )
                    await db.execute(f"PRAGMA user_version = {self.version}")
                    await db.commit()
        self.db_initialized.set()

    async def read(self) -> AsyncIterator[tuple[bytes, bytes, float]]:  # type: ignore
        """用于读取存储内容的异步迭代器。

        Returns:
            A tuple of (update, metadata, timestamp) for each update.
        """
        await self.db_initialized.wait()
        try:
            async with self.lock:
                async with aiosqlite.connect(self.db_path) as db:
                    async with db.execute(
                        "SELECT yupdate, metadata, timestamp FROM yupdates WHERE path = ?",
                        (self.path,),
                    ) as cursor:
                        found = False
                        async for update, metadata, timestamp in cursor:
                            found = True
                            yield update, metadata, timestamp
                        if not found:
                            raise YDocNotFound
        except Exception:
            raise YDocNotFound

    async def write(self, data: bytes) -> None:
        """保存更新。

        Arguments:
            data: 要存储的更新。
        """
        await self.db_initialized.wait()
        async with self.lock:
            async with aiosqlite.connect(self.db_path) as db:
                # 首先，确定自上次更新以来经过的时间
                cursor = await db.execute(
                    "SELECT timestamp FROM yupdates WHERE path = ? ORDER BY timestamp DESC LIMIT 1",
                    (self.path,),
                )
                row = await cursor.fetchone()
                diff = (time.time() - row[0]) if row else 0

                if self.document_ttl is not None and diff > self.document_ttl:
                    # squash updates
                    ydoc = Y.YDoc()
                    async with db.execute(
                        "SELECT yupdate FROM yupdates WHERE path = ?", (self.path,)
                    ) as cursor:
                        async for (update,) in cursor:
                            Y.apply_update(ydoc, update)
                    # delete history
                    await db.execute(
                        "DELETE FROM yupdates WHERE path = ?", (self.path,)
                    )
                    # insert squashed updates
                    squashed_update = Y.encode_state_as_update(ydoc)
                    metadata = await self.get_metadata()
                    await db.execute(
                        "INSERT INTO yupdates VALUES (?, ?, ?, ?)",
                        (self.path, squashed_update, metadata, time.time()),
                    )

                # finally, write this update to the DB
                metadata = await self.get_metadata()
                await db.execute(
                    "INSERT INTO yupdates VALUES (?, ?, ?, ?)",
                    (self.path, data, metadata, time.time()),
                )
                await db.commit()
