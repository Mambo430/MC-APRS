import asyncio
import logging
import threading
import time

from meshcore import EventType, MeshCore

logger = logging.getLogger("mc_aprs")
RECONNECT_DELAY = 5
FLOOD_ADVERT_INTERVAL = 12 * 60 * 60


class MeshCoreInterface:
    def __init__(self, device, baudrate, on_message, flood_advert=False):
        self._device = device
        self._baudrate = baudrate
        self._on_message = on_message
        self._flood_advert = flood_advert
        self._loop = None
        self._loop_ready = threading.Event()
        self._thread = None
        self._client = None
        self._gateway_id = None
        self._monitor_task = None
        self._flood_advert_task = None
        self._last_flood_advert_at = None
        self._stopping = False

    def connect(self):
        self._thread = threading.Thread(target=self._run_event_loop, daemon=True)
        self._thread.start()
        if not self._loop_ready.wait(timeout=10):
            self.close()
            raise TimeoutError("Timed out starting the MeshCore event loop.")

        future = asyncio.run_coroutine_threadsafe(self._connect(), self._loop)
        try:
            future.result(timeout=30)
        except BaseException:
            future.cancel()
            self.close()
            raise
        logger.info(f"Connected to MeshCore Companion on {self._device}")
        return self

    async def _connect(self):
        self._client = await self._create_client()
        self._monitor_task = asyncio.create_task(self._monitor_connection())
        if self._flood_advert:
            self._flood_advert_task = asyncio.create_task(self._send_flood_adverts())

    async def _create_client(self):
        client = await MeshCore.create_serial(
            self._device,
            baudrate=self._baudrate,
            auto_reconnect=False,
        )
        if client is None:
            raise ConnectionError(
                f"Unable to connect to MeshCore Companion on {self._device}."
            )

        try:
            public_key = client.self_info.get("public_key", "")
            if len(public_key) != 64 or any(
                char not in "0123456789abcdefABCDEF" for char in public_key
            ):
                raise ConnectionError(
                    "MeshCore Companion did not provide a valid public key."
                )
            self._gateway_id = "!" + public_key[:12].lower()

            await client.ensure_contacts()
            client.subscribe(EventType.CONTACT_MSG_RECV, self._handle_message)
            client.subscribe(EventType.CHANNEL_MSG_RECV, self._handle_channel_message)
            await client.start_auto_message_fetching()
        except BaseException:
            await client.disconnect()
            raise
        return client

    async def _monitor_connection(self):
        while not self._stopping:
            await asyncio.sleep(1)
            if self._client is None or self._client.is_connected:
                continue

            logger.warning(
                f"MeshCore Companion disconnected from {self._device}; " "reconnecting."
            )
            client = self._client
            self._client = None
            try:
                await client.disconnect()
            except Exception:
                logger.exception("Error closing disconnected MeshCore client.")

            while not self._stopping:
                try:
                    self._client = await self._create_client()
                    logger.info(f"Reconnected to MeshCore Companion on {self._device}")
                    break
                except Exception:
                    logger.exception(
                        f"Unable to reconnect to MeshCore Companion on {self._device}; "
                        f"retrying in {RECONNECT_DELAY} seconds."
                    )
                    await asyncio.sleep(RECONNECT_DELAY)

    async def _send_flood_adverts(self):
        while not self._stopping:
            client = self._client
            if client is None or not client.is_connected:
                await asyncio.sleep(RECONNECT_DELAY)
                continue

            try:
                sent = await self._send_flood_advert()
            except Exception:
                logger.exception("Unable to send MeshCore Flood Advert; retrying.")
                await asyncio.sleep(RECONNECT_DELAY)
                continue

            if sent:
                await asyncio.sleep(FLOOD_ADVERT_INTERVAL)
            else:
                remaining = FLOOD_ADVERT_INTERVAL - (
                    time.monotonic() - self._last_flood_advert_at
                )
                await asyncio.sleep(max(remaining, 1))

    async def _send_flood_advert(self, force=False):
        now = time.monotonic()
        if (
            not force
            and self._last_flood_advert_at is not None
            and now - self._last_flood_advert_at < FLOOD_ADVERT_INTERVAL
        ):
            logger.debug(
                "Skipping Flood Advert; one was sent within the last 12 hours."
            )
            return False

        if self._client is None or not self._client.is_connected:
            raise ConnectionError("MeshCore Companion is not connected.")

        result = await self._client.commands.send_advert(flood=True)
        if result is None or result.type == EventType.ERROR:
            raise RuntimeError(f"MeshCore failed to send Flood Advert: {result}")
        self._last_flood_advert_at = time.monotonic()
        logger.info("Sent MeshCore Flood Advert.")
        return True

    def _run_event_loop(self):
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        self._loop_ready.set()
        self._loop.run_forever()

        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(
                asyncio.gather(*pending, return_exceptions=True)
            )
        self._loop.close()

    def _handle_message(self, event):
        payload = event.payload
        if not isinstance(payload, dict):
            logger.warning("Ignoring MeshCore message with an invalid payload.")
            return

        if payload.get("txt_type", 0) != 0:
            return

        public_key_prefix = payload.get("pubkey_prefix", "").lower()
        if len(public_key_prefix) != 12 or any(
            char not in "0123456789abcdef" for char in public_key_prefix
        ):
            logger.warning(
                "Ignoring MeshCore message with invalid sender public-key prefix."
            )
            return

        message = payload.get("text")
        if not isinstance(message, str):
            logger.warning("Ignoring MeshCore message with no text payload.")
            return

        packet = {
            "fromId": "!" + public_key_prefix,
            "toId": self._gateway_id,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": message.encode("utf-8"),
            },
        }
        self._on_message(packet)

    def _handle_channel_message(self, event):
        payload = event.payload
        if not isinstance(payload, dict):
            logger.warning("Ignoring MeshCore channel message with an invalid payload.")
            return
        channel_index = payload.get("channel_idx")
        text_type = payload.get("txt_type", 0)
        if channel_index != 0 or text_type != 0:
            logger.debug(
                "Ignoring MeshCore channel message (channel %s, text type %s).",
                channel_index,
                text_type,
            )
            return

        message = payload.get("text")
        if not isinstance(message, str):
            logger.warning("Ignoring MeshCore channel message with no text payload.")
            return

        self._on_message(
            {
                "decoded": {
                    "portnum": "MESHCORE_CHANNEL_TEXT_MESSAGE",
                    "channel_idx": 0,
                    "payload": message.encode("utf-8"),
                }
            }
        )

    @property
    def is_connected(self):
        return self._client is not None and self._client.is_connected

    def getMyNodeInfo(self):
        return {"user": {"id": self._gateway_id}, "position": {}}

    def getContacts(self):
        if self._client is None or self._loop is None:
            raise ConnectionError("MeshCore Companion is not connected.")

        future = asyncio.run_coroutine_threadsafe(self._get_contacts(), self._loop)
        return future.result(timeout=10)

    async def _get_contacts(self):
        return list(self._client.contacts.values())

    def sendText(self, text, destinationId, wantAck=True, wantResponse=False):
        if self._client is None or self._loop is None:
            raise ConnectionError("MeshCore Companion is not connected.")

        public_key_prefix = destinationId.removeprefix("!").lower()
        future = asyncio.run_coroutine_threadsafe(
            self._send_message(public_key_prefix, text), self._loop
        )
        future.result(timeout=30)

    def sendChannelText(self, text, channelIndex=0):
        if self._client is None or self._loop is None:
            raise ConnectionError("MeshCore Companion is not connected.")

        future = asyncio.run_coroutine_threadsafe(
            self._send_channel_message(channelIndex, text), self._loop
        )
        future.result(timeout=30)

    async def _send_channel_message(self, channel_index, text):
        result = await self._client.commands.send_chan_msg(channel_index, text)
        if result is None or result.type == EventType.ERROR:
            raise RuntimeError(f"MeshCore failed to send channel message: {result}")

    def sendFloodAdvert(self):
        if self._client is None or self._loop is None:
            raise ConnectionError("MeshCore Companion is not connected.")

        future = asyncio.run_coroutine_threadsafe(
            self._send_flood_advert(force=True), self._loop
        )
        future.result(timeout=30)

    async def _send_message(self, public_key_prefix, text):
        contact = self._client.get_contact_by_key_prefix(public_key_prefix)
        if contact is None:
            raise ValueError(
                f"MeshCore contact '{public_key_prefix}' is not in the Companion contact list."
            )
        result = await self._client.commands.send_msg(contact, text)
        if result is None or result.type == EventType.ERROR:
            raise RuntimeError(f"MeshCore failed to send message: {result}")

    def close(self):
        if self._loop is None or self._loop.is_closed():
            return

        self._stopping = True
        try:
            future = asyncio.run_coroutine_threadsafe(self._disconnect(), self._loop)
            future.result(timeout=10)
        finally:
            self._loop.call_soon_threadsafe(self._loop.stop)
            if self._thread is not None:
                self._thread.join(timeout=10)

    async def _disconnect(self):
        tasks = [
            task
            for task in (self._monitor_task, self._flood_advert_task)
            if task is not None
        ]
        for task in tasks:
            task.cancel()
        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        if self._client is not None:
            await self._client.disconnect()
            self._client = None
