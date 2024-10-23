import logging
import asyncio
import uvicorn
from typing import AsyncGenerator
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, Depends, WebSocketDisconnect


from ypy_websocket import WebsocketServer as Y_WebsocketServer
from ypy_websocket.websocket import Websocket as Y_WebsocketProtocol

lifespan_dict = {}


@asynccontextmanager
async def lifespan(_application: FastAPI) -> AsyncGenerator:
    # Startup
    global lifespan_dict

    y_ws_server = Y_WebsocketServer()

    await y_ws_server.start()

    lifespan_dict["y_ws_server"] = y_ws_server  # 注册y_ws_server连接

    yield

    # Shutdown
    y_ws_server.stop()


def get_y_ws_server() -> Y_WebsocketServer:
    global lifespan_dict

    return lifespan_dict["y_ws_server"]


class Y_Websocket(Y_WebsocketProtocol):
    def __init__(self, ws: WebSocket) -> None:
        self._ws_inst = ws

    @property
    def path(self) -> str:
        """WebSocket 路径"""

        return self._ws_inst.url.path

    async def send(self, message: bytes) -> None:
        """发送消息。

        Arguments:
            message: 要发送的消息。
        """
        await self._ws_inst.send_bytes(message)

    async def recv(self) -> bytes:
        """收到一条消息。

        Returns:
            收到的消息。
        """

        return await self._ws_inst.receive_bytes()


app = FastAPI(lifespan=lifespan)
# app = FastAPI()


@app.websocket("/ws")  # 老师授课连接
@app.websocket("/ws/{editname}/{room_name}")  # 老师授课连接
async def ws(
    websocket: WebSocket,
    editname: str,
    room_name: str,
    y_ws_server: Y_WebsocketServer = Depends(get_y_ws_server),
):
    """
    接收websoket连接
    """

    try:
        await websocket.accept()
        # await websocket.accept("prosemirror-demo-2024/06")

        await y_ws_server.serve(Y_Websocket(websocket))

        await asyncio.Future()  # 永久运行

    except WebSocketDisconnect:
        print(f"{websocket.url.hostname}: {websocket.url.path} 断开连接")

    except asyncio.exceptions.CancelledError:
        print("关闭websocket服务")


if __name__ == "__main__":
    # uvicorn.run(app, host="127.0.0.1", port=8001)  # 这种情况 不能设置 reload 或 workers
    uvicorn.run(
        "fmain:app",
        host="0.0.0.0",
        port=8008,
        # reload=True,
        # log_level=logging.DEBUG,
    )
