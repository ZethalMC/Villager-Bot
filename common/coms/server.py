import asyncio
import logging
from typing import Callable, Optional

from pydantic import BaseModel
from websockets.server import WebSocketServerProtocol, serve

from common.coms.coms_base import ComsBase
from common.coms.errors import InvalidPacketReceived
from common.coms.packet import T_PACKET_DATA, Packet
from common.coms.packet_handling import PacketHandler, PacketType


class Broadcast(BaseModel):
    ready: asyncio.Event
    ws_ids: set[int]  # ws ids to expect a response from
    responses: list[T_PACKET_DATA]

    class Config:
        arbitrary_types_allowed = True


class Server(ComsBase):
    def __init__(
        self,
        host: str,
        port: int,
        auth: str,
        packet_handlers: dict[PacketType, PacketHandler],
        logger: logging.Logger,
    ):
        super().__init__(host, port, packet_handlers, logger.getChild("server"))

        self.auth = auth

        self._stop = asyncio.Event()
        self._connections = list[WebSocketServerProtocol]()  # only authed connections
        self._current_id = 0
        self._broadcasts = dict[str, Broadcast]()

    def _get_packet_id(self, t: str = "s") -> str:
        packet_id = self._current_id
        self._current_id += 1
        return f"{t}{packet_id}"

    async def serve(self, ready_cb: Optional[Callable[[], None]]) -> None:
        async with serve(self._handle_connection, self.host, self.port, logger=self.logger):
            ready_cb()
            await self._stop.wait()

    async def _send(self, ws: WebSocketServerProtocol, packet: Packet) -> None:
        await ws.send(packet.json())

    async def _disconnect(self, ws: WebSocketServerProtocol) -> None:
        await ws.close()

        try:
            self._connections.remove(ws)
        except ValueError:
            pass

        self.logger.info("Disconnected client: %s", ws.id)

    async def broadcast(
        self, packet_type: PacketType, packet_data: T_PACKET_DATA = None
    ) -> list[T_PACKET_DATA]:
        if len(self._connections) == 0:
            raise RuntimeError("There are no connected clients to broadcast to.")

        broadcast_id = self._get_packet_id("b")
        broadcast_packet = Packet(id=broadcast_id, type=packet_type, data=packet_data)

        ws_ids = {ws.id for ws in self._connections}
        broadcast_coros = [
            self._send(c, broadcast_packet) for c in self._connections if c.id in ws_ids
        ]
        broadcast = self._broadcasts[broadcast_id] = Broadcast(
            ready=asyncio.Event(), ws_ids=ws_ids, responses=[]
        )

        await asyncio.wait(broadcast_coros)
        await broadcast.ready.wait()

        return broadcast.responses

    async def _client_broadcast(self, ws: WebSocketServerProtocol, packet: Packet) -> None:
        responses = await self.broadcast(packet.data["type"], packet.data["data"])

        # send response back to client who requested broadcast
        await self._send(ws, Packet(id=packet.id, data=responses))

    async def _handle_broadcast_response(self, ws: WebSocketServerProtocol, packet: Packet) -> None:
        broadcast = self._broadcasts[packet.id]

        if ws.id not in broadcast.ws_ids:
            raise RuntimeError(
                f"Unexpected response from websocket {ws.id} for broadcast {packet.id}",
            )

        broadcast.responses.append(packet.data)
        broadcast.ws_ids.remove(ws.id)

        if len(broadcast.ws_ids) == 0:
            broadcast.ready.set()

    async def _handle_connection(self, ws: WebSocketServerProtocol):
        self.logger.info("New client from %s:%s connected: %s", ws.host, ws.port, ws.id)

        authed = False

        async for message in ws:
            try:
                packet = self._decode(message)
            except InvalidPacketReceived:
                self.logger.error("Invalid packet received from client: %s", ws.id, exc_info=True)
                await self._disconnect(ws)
                return

            if packet.type == PacketType.AUTH:
                if authed:
                    self.logger.error(
                        "Already received authorization packet from client: %s", ws.id
                    )
                    await self._disconnect(ws)
                    return

                if packet.get("auth") != self.auth:
                    await self._disconnect(ws)
                    self.logger.error("Incorrect authorization received from client: %s", ws.id)
                    return

                self._connections.append(ws)
                authed = True

            if not authed:
                self.logger.error(
                    "Authorization packet was not the first received from client: %s", ws.id
                )
                await self._disconnect(ws)
                return

            # handle broadcast requests
            if packet.type == PacketType.BROADCAST_REQUEST:
                # broadcast requests are special types of packets which forward the packet to ALL connected clients
                asyncio.create_task(
                    self._client_broadcast(ws, packet)
                )  # TODO: keep track of these tasks and properly cancel them on a server shutdown
                continue

            # handle broadcast responses
            if packet.id in self._broadcasts:
                await self._handle_broadcast_response(ws, packet)
                continue

            try:
                response = await self._call_handler(packet)
            except Exception as e:
                self.logger.error(
                    "An error ocurred while calling the packet handler for packet %s",
                    packet,
                    exc_info=True,
                )
                # TODO: Send some sort of error packet, since response isn't sent
                await self._send(ws, Packet(id=packet.id, data=str(e), error=True))
            else:
                await self._send(ws, Packet(id=packet.id, data=response))