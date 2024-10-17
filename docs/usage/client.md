

=== "中文"

    客户端通过 [WebsocketProvider](../reference/WebSocket_provider.md) 连接他们的 `YDoc`。

    以下是使用 [websockets](https://websockets.readthedocs.io) 库的代码示例：
    
    ```py
    import asyncio
    import y_py as Y
    from websockets import connect
    from ypy_websocket import WebsocketProvider
    
    async def client():
        ydoc = Y.YDoc()
        async with (
            connect("ws://localhost:1234/my-roomname") as websocket,
            WebsocketProvider(ydoc, websocket),
        ):
            # 对远程 ydoc 的更改会应用到本地 ydoc。
            # 对本地 ydoc 的更改会通过 WebSocket 发送
            # 并广播到所有客户端。
            ymap = ydoc.get_map("map")
            with ydoc.begin_transaction() as t:
                ymap.set(t, "key", "value")
    
            await asyncio.Future()  # 永久运行
    
    asyncio.run(client())
    ```

=== "英文"

    A client connects their `YDoc` through a [WebsocketProvider](../reference/WebSocket_provider.md).
    
    Here is a code example using the [websockets](https://websockets.readthedocs.io) library:
    ```py
    import asyncio
    import y_py as Y
    from websockets import connect
    from ypy_websocket import WebsocketProvider
    
    async def client():
        ydoc = Y.YDoc()
        async with (
            connect("ws://localhost:1234/my-roomname") as websocket,
            WebsocketProvider(ydoc, websocket),
        ):
            # Changes to remote ydoc are applied to local ydoc.
            # Changes to local ydoc are sent over the WebSocket and
            # broadcast to all clients.
            ymap = ydoc.get_map("map")
            with ydoc.begin_transaction() as t:
                ymap.set(t, "key", "value")
    
            await asyncio.Future()  # run forever
    
    asyncio.run(client())
    ```
