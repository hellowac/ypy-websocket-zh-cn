import sys

if sys.version_info >= (3, 8):
    from typing import Protocol
else:
    from typing_extensions import Protocol


class Websocket(Protocol):
    """WebSocket.

    Websocket 实例可以使用异步迭代器接收消息，直到连接关闭：

    ```py
    async for message in websocket:
        ...
    ```

    或者通过直接调用 `recv()`：

    ```py
    message = await websocket.recv()
    ```

    发送消息使用 `send()`：

    ```py
    await websocket.send(message)
    ```
    """

    @property
    def path(self) -> str:
        """WebSocket 路径"""
        ...

    def __aiter__(self):
        return self

    async def __anext__(self) -> bytes:
        try:
            message = await self.recv()
        except Exception:
            raise StopAsyncIteration()

        return message

    async def send(self, message: bytes) -> None:
        """发送消息。

        Arguments:
            message: 要发送的消息。
        """
        ...

    async def recv(self) -> bytes:
        """收到一条消息。

        Returns:
            收到的消息。
        """
        ...
