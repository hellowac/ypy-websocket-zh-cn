from __future__ import annotations

from logging import getLogger
from typing import TypedDict

import y_py as Y
from channels.generic.websocket import AsyncWebsocketConsumer  # type: ignore

from .websocket import Websocket
from .yutils import YMessageType, process_sync_message, sync

logger = getLogger(__name__)


class _WebsocketShim(Websocket):
    def __init__(self, path, send_func) -> None:
        self._path = path
        self._send_func = send_func

    @property
    def path(self) -> str:
        return self._path

    def __aiter__(self):
        raise NotImplementedError()

    async def __anext__(self) -> bytes:
        raise NotImplementedError()

    async def send(self, message: bytes) -> None:
        await self._send_func(message)

    async def recv(self) -> bytes:
        raise NotImplementedError()


class YjsConsumer(AsyncWebsocketConsumer):
    """一个适用于 [Django Channels](https://github.com/django/channels) 的工作消费者。

    该消费者可以开箱即用，只需添加：
    ```py
    path("ws/<str:room>", YjsConsumer.as_asgi())
    ```
    到您的 `urls.py` 文件中。在实际操作中，一旦您
    [设置 Channels](https://channels.readthedocs.io/en/1.x/getting-started.html)，您可能会有如下内容：
    ```py
    # urls.py
    from django.urls import path
    from backend.consumer import DocConsumer, UpdateConsumer

    urlpatterns = [
        path("ws/<str:room>", YjsConsumer.as_asgi()),
    ]

    # asgi.py
    import os
    from channels.routing import ProtocolTypeRouter, URLRouter
    from urls import urlpatterns

    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "backend.settings")

    application = ProtocolTypeRouter({
        "websocket": URLRouter(urlpatterns_ws),
    })
    ```

    此外，可以通过子类化该消费者来自定义其行为。

    特别地，

    - 重写 `make_room_name` 以自定义房间名称。
    - 重写 `make_ydoc` 以初始化 YDoc。这对于使用数据库中的数据初始化它或为其添加观察者很有用。
    - 重写 `connect` 以在连接时进行自定义验证（例如身份验证），但一定要在最后调用 `await super().connect()`。
    - 调用 `group_send_message` 向整个组/房间发送消息。
    - 调用 `send_message` 向单个客户端发送消息，尽管不推荐这样做。

    展示所有这些选项的自定义消费者的完整示例是：

    ```py
    import y_py as Y
    from asgiref.sync import async_to_sync
    from channels.layers import get_channel_layer
    from ypy_websocket.django_channels_consumer import YjsConsumer
    from ypy_websocket.yutils import create_update_message


    class DocConsumer(YjsConsumer):
        def make_room_name(self) -> str:
            # 在此修改房间名称
            return self.scope["url_route"]["kwargs"]["room"]

        async def make_ydoc(self) -> Y.YDoc:
            doc = Y.YDoc()
            # 在此用数据库中的数据填充 doc
            doc.observe_after_transaction(self.on_update_event)
            return doc

        async def connect(self):
            user = self.scope["user"]
            if user is None or user.is_anonymous:
                await self.close()
                return
            await super().connect()

        def on_update_event(self, event):
            # 在此处理事件
            ...

        async def doc_update(self, update_wrapper):
            update = update_wrapper["update"]
            Y.apply_update(self.ydoc, update)
            await self.group_send_message(create_update_message(update))


    def send_doc_update(room_name, update):
        layer = get_channel_layer()
        async_to_sync(layer.group_send)(room_name, {"type": "doc_update", "update": update})
    ```

    """

    def __init__(self):
        super().__init__()
        self.room_name = None
        self.ydoc = None
        self._websocket_shim = None

    def make_room_name(self) -> str:
        """为新通道创建房间名称。

        重写此方法以自定义创建通道时的房间名称。

        Returns:
            新通道的房间名称。默认值为 URL 路由中的房间名称。
        """
        return self.scope["url_route"]["kwargs"]["room"]

    async def make_ydoc(self) -> Y.YDoc:
        """为新通道创建 YDoc。

        重写此方法以自定义创建通道时的 YDoc（这对于用数据库中的数据初始化它或为其添加观察者很有用）。

        Returns:
            新通道的 YDoc。默认值为一个新的空 YDoc。
        """
        return Y.YDoc()

    def _make_websocket_shim(self, path: str) -> _WebsocketShim:
        return _WebsocketShim(path, self.group_send_message)

    async def connect(self) -> None:
        self.room_name = self.make_room_name()
        self.ydoc = await self.make_ydoc()
        self._websocket_shim = self._make_websocket_shim(self.scope["path"])

        await self.channel_layer.group_add(self.room_name, self.channel_name)
        await self.accept()

        await sync(self.ydoc, self._websocket_shim, logger)

    async def disconnect(self, code) -> None:
        await self.channel_layer.group_discard(self.room_name, self.channel_name)

    async def receive(self, text_data=None, bytes_data=None):
        if bytes_data is None:
            return
        await self.group_send_message(bytes_data)
        if bytes_data[0] != YMessageType.SYNC:
            return
        await process_sync_message(
            bytes_data[1:], self.ydoc, self._websocket_shim, logger
        )

    class WrappedMessage(TypedDict):
        """发送给客户端的包装消息。"""

        message: bytes

    async def send_message(self, message_wrapper: WrappedMessage) -> None:
        """发送消息给客户端。

        Arguments:
            message_wrapper: 要发送的消息，已包装。
        """
        await self.send(bytes_data=message_wrapper["message"])

    async def group_send_message(self, message: bytes) -> None:
        """向群组发送消息。

        Arguments:
            message: 要发送的消息。
        """
        await self.channel_layer.group_send(
            self.room_name, {"type": "send_message", "message": message}
        )
