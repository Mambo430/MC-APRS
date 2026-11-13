import asyncio
import threading
import time
from types import SimpleNamespace

import pytest
from meshcore import EventType

from mc_aprs._meshcore_interface import (
    FLOOD_ADVERT_INTERVAL,
    MeshCoreInterface,
)


def test_handle_message_normalizes_meshcore_sender_id():
    received = []
    interface = MeshCoreInterface("/dev/ttyACM0", 115200, received.append)
    interface._gateway_id = "!112233445566"

    interface._handle_message(
        SimpleNamespace(
            payload={"pubkey_prefix": "A1B2C3D4E5F6", "text": "WLNK-1: hello"}
        )
    )

    assert received == [
        {
            "fromId": "!a1b2c3d4e5f6",
            "toId": "!112233445566",
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": b"WLNK-1: hello",
            },
        }
    ]


def test_handle_message_ignores_invalid_public_key_prefix():
    received = []
    interface = MeshCoreInterface("/dev/ttyACM0", 115200, received.append)

    interface._handle_message(
        SimpleNamespace(payload={"pubkey_prefix": "not-a-key", "text": "hello"})
    )

    assert received == []


def test_handle_channel_message_forwards_only_plain_channel_zero_text():
    received = []
    interface = MeshCoreInterface("/dev/ttyACM0", 115200, received.append)

    interface._handle_channel_message(
        SimpleNamespace(payload={"channel_idx": 0, "txt_type": 0, "text": "APRS?"})
    )
    interface._handle_channel_message(
        SimpleNamespace(payload={"channel_idx": 1, "txt_type": 0, "text": "aprs?"})
    )
    interface._handle_channel_message(
        SimpleNamespace(payload={"channel_idx": 0, "txt_type": 1, "text": "aprs?"})
    )

    assert received == [
        {
            "decoded": {
                "portnum": "MESHCORE_CHANNEL_TEXT_MESSAGE",
                "channel_idx": 0,
                "payload": b"APRS?",
            }
        }
    ]


def test_send_message_uses_meshcore_contact_prefix():
    contact = {"public_key": "a1b2c3d4e5f6"}
    sent = []

    class Commands:
        async def send_msg(self, destination, text):
            sent.append((destination, text))
            return SimpleNamespace(type=EventType.MSG_SENT)

    class Client:
        commands = Commands()

        def get_contact_by_key_prefix(self, prefix):
            assert prefix == "a1b2c3d4e5f6"
            return contact

    interface = MeshCoreInterface("/dev/ttyACM0", 115200, lambda packet: None)
    interface._client = Client()

    asyncio.run(interface._send_message("a1b2c3d4e5f6", "reply"))

    assert sent == [(contact, "reply")]


def test_send_message_reports_missing_contact():
    class Commands:
        async def send_msg(self, destination, text):
            pytest.fail("send_msg must not be called without a contact")

    class Client:
        commands = Commands()

        def get_contact_by_key_prefix(self, prefix):
            return None

    interface = MeshCoreInterface("/dev/ttyACM0", 115200, lambda packet: None)
    interface._client = Client()

    with pytest.raises(ValueError, match="not in the Companion contact list"):
        asyncio.run(interface._send_message("a1b2c3d4e5f6", "reply"))


def test_send_channel_message_uses_requested_channel():
    sent = []

    class Commands:
        async def send_chan_msg(self, channel, text):
            sent.append((channel, text))
            return SimpleNamespace(type=EventType.OK)

    interface = MeshCoreInterface("/dev/ttyACM0", 115200, lambda packet: None)
    interface._client = SimpleNamespace(commands=Commands())

    asyncio.run(interface._send_channel_message(0, "online"))

    assert sent == [(0, "online")]


def test_flood_advert_skips_if_recently_sent():
    class Commands:
        async def send_advert(self, flood):
            pytest.fail("A recent Flood Advert should be rate-limited.")

    interface = MeshCoreInterface("/dev/ttyACM0", 115200, lambda packet: None)
    interface._client = SimpleNamespace(
        commands=Commands(),
        is_connected=True,
    )
    interface._last_flood_advert_at = time.monotonic()

    assert asyncio.run(interface._send_flood_advert()) is False


def test_forced_flood_advert_sends_even_if_recently_sent():
    adverts = []

    class Commands:
        async def send_advert(self, flood):
            adverts.append(flood)
            return SimpleNamespace(type=EventType.OK)

    interface = MeshCoreInterface("/dev/ttyACM0", 115200, lambda packet: None)
    interface._client = SimpleNamespace(
        commands=Commands(),
        is_connected=True,
    )
    interface._last_flood_advert_at = time.monotonic()

    assert asyncio.run(interface._send_flood_advert(force=True)) is True
    assert adverts == [True]


def test_connect_and_close_runs_meshcore_on_background_event_loop(monkeypatch):
    class Client:
        self_info = {"public_key": "a1" * 32}
        contacts = {"contact": {"public_key": "b2" * 32}}
        is_connected = True

        async def ensure_contacts(self):
            pass

        def subscribe(self, event_type, callback):
            assert event_type in (
                EventType.CONTACT_MSG_RECV,
                EventType.CHANNEL_MSG_RECV,
            )

        async def start_auto_message_fetching(self):
            pass

        async def disconnect(self):
            self.is_connected = False

    client = Client()

    class FakeMeshCore:
        @staticmethod
        async def create_serial(device, baudrate, auto_reconnect):
            assert (device, baudrate, auto_reconnect) == (
                "/dev/ttyACM0",
                115200,
                False,
            )
            return client

    monkeypatch.setattr("mc_aprs._meshcore_interface.MeshCore", FakeMeshCore)
    interface = MeshCoreInterface("/dev/ttyACM0", 115200, lambda packet: None)

    assert interface.connect() is interface
    assert interface.getMyNodeInfo()["user"]["id"] == "!a1a1a1a1a1a1"
    assert interface.getContacts() == list(client.contacts.values())

    interface.close()

    assert not interface.is_connected
    assert not interface._thread.is_alive()


def test_flood_advert_is_sent_on_connect_and_periodically(monkeypatch):
    adverts = []
    two_adverts_sent = threading.Event()

    class Commands:
        async def send_advert(self, flood):
            adverts.append(flood)
            if len(adverts) >= 2:
                two_adverts_sent.set()
            return SimpleNamespace(type=EventType.OK)

    class Client:
        self_info = {"public_key": "a1" * 32}
        contacts = {}
        commands = Commands()
        is_connected = True

        async def ensure_contacts(self):
            pass

        def subscribe(self, event_type, callback):
            pass

        async def start_auto_message_fetching(self):
            pass

        async def disconnect(self):
            self.is_connected = False

    client = Client()

    class FakeMeshCore:
        @staticmethod
        async def create_serial(device, baudrate, auto_reconnect):
            return client

    monkeypatch.setattr("mc_aprs._meshcore_interface.MeshCore", FakeMeshCore)
    monkeypatch.setattr(
        "mc_aprs._meshcore_interface.FLOOD_ADVERT_INTERVAL",
        0.02,
    )
    interface = MeshCoreInterface(
        "/dev/ttyACM0", 115200, lambda packet: None, flood_advert=True
    )

    try:
        interface.connect()
        assert two_adverts_sent.wait(timeout=1)
    finally:
        interface.close()

    assert FLOOD_ADVERT_INTERVAL == 12 * 60 * 60
    assert len(adverts) >= 2
    assert all(adverts)
