
=== "中文"

    传递给 `WebsocketProvider` 和 `WebsocketServer.serve` 的 WebSocket 对象必须实现以下 API，该 API 定义为 [协议类](../../reference/WebSocket)：
    
    ```py
    class WebSocket:
    
        @property
        def path(self) -> str:
            # 可以是例如 URL 路径
            # 或房间标识符
            return "my-roomname"
    
        def __aiter__(self):
            return self
    
        async def __anext__(self) -> bytes:
            # 用于接收消息的异步迭代器
            # 直到连接关闭
            try:
                message = await self.recv()
            except Exception:
                raise StopAsyncIteration()
    
            return message
    
        async def send(self, message: bytes):
            # 发送消息
            pass
    
        async def recv(self) -> bytes:
            # 接收消息
            return b""
    ```

=== "英文"

    The WebSocket object passed to `WebsocketProvider` and `WebsocketServer.serve` must implement the following API, defined as a [protocol class](../../reference/WebSocket):
    
    ```py
    class WebSocket:
    
        @property
        def path(self) -> str:
            # can be e.g. the URL path
            # or a room identifier
            return "my-roomname"
    
        def __aiter__(self):
            return self
    
        async def __anext__(self) -> bytes:
            # async iterator for receiving messages
            # until the connection is closed
            try:
                message = await self.recv()
            except Exception:
                raise StopAsyncIteration()
    
            return message
    
        async def send(self, message: bytes):
            # send message
            pass
    
        async def recv(self) -> bytes:
            # receive message
            return b""
    ```
    