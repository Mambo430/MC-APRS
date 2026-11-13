import pytest

from mc_aprs._gateway import Gateway, REGISTRATION_BEACON


@pytest.mark.parametrize("device_id", ["!abcdef01", "!abcdef012345"])
def test_registration_beacon_accepts_legacy_and_meshcore_ids(device_id):
    class Registry:
        def __init__(self):
            self.registrations = []

        def add_registration(self, device_id, call_sign, icon, is_local):
            self.registrations.append((device_id, call_sign, icon, is_local))

    gateway = Gateway.__new__(Gateway)
    gateway._registry = Registry()

    gateway._process_aprs_packet(
        {
            "format": "message",
            "from": "N0CALL-1",
            "addresse": REGISTRATION_BEACON,
            "message_text": device_id,
        }
    )

    assert gateway._registry.registrations == [(device_id, "N0CALL-1", None, False)]


def test_get_interface_configures_tcp_connection(monkeypatch):
    created = {}

    class FakeMeshCoreInterface:
        def __init__(
            self,
            endpoint,
            baudrate,
            on_message,
            flood_advert=False,
            connection_type="serial",
            port=None,
        ):
            created.update(
                endpoint=endpoint,
                baudrate=baudrate,
                on_message=on_message,
                flood_advert=flood_advert,
                connection_type=connection_type,
                port=port,
            )

        def connect(self):
            return self

    monkeypatch.setattr("mc_aprs._gateway.MeshCoreInterface", FakeMeshCoreInterface)
    gateway = Gateway.__new__(Gateway)
    on_message = lambda packet: None

    interface = gateway._get_interface(
        {
            "type": "tcp",
            "host": "192.168.1.50",
            "port": 5000,
            "flood_advert": True,
        },
        on_message,
    )

    assert interface is not None
    assert created == {
        "endpoint": "192.168.1.50",
        "baudrate": 115200,
        "on_message": on_message,
        "flood_advert": True,
        "connection_type": "tcp",
        "port": 5000,
    }


@pytest.mark.parametrize(
    ("config", "message"),
    [
        ({"type": "tcp", "host": "192.168.1.50"}, "meshcore_interface.port"),
        ({"type": "tcp", "port": 5000}, "meshcore_interface.host"),
        ({"type": "bluetooth"}, "meshcore_interface.type"),
    ],
)
def test_get_interface_rejects_invalid_connection_config(config, message):
    gateway = Gateway.__new__(Gateway)

    with pytest.raises(ValueError, match=message):
        gateway._get_interface(config, lambda packet: None)


def test_aprs_service_error_is_forwarded_to_registered_meshcore_contact():
    gateway = Gateway.__new__(Gateway)
    gateway._registry = {"!abcdef012345": {"call_sign": "KQ4WAA-7"}}
    gateway._gateway_call_sign = "MCGATE"
    gateway._filtered_call_signs = ["KQ4WAA-7"]
    gateway._reply_to = {}
    gateway._aprs_client = None
    acknowledged = []
    sent_to_mesh = []
    gateway._send_aprs_ack = lambda *args: acknowledged.append(args)
    gateway._send_mesh_message = lambda *args: sent_to_mesh.append(args)

    gateway._process_aprs_packet(
        {
            "format": "message",
            "from": "WXBOT",
            "addresse": "KQ4WAA-7",
            "message_text": "Unable to find location for KQ4WAA-7",
            "msgNo": "42",
        }
    )

    assert acknowledged == [("KQ4WAA-7", "WXBOT", "42")]
    assert sent_to_mesh == [
        ("!abcdef012345", "WXBOT: Unable to find location for KQ4WAA-7")
    ]


@pytest.mark.parametrize("registered", [False, True])
def test_help_reply_fits_meshcore_message_limit(registered):
    sender_id = "!abcdef012345"
    gateway = Gateway.__new__(Gateway)
    gateway._gateway_id = "!123456789abc"
    gateway._registry = {sender_id: {"call_sign": "KQ4WAA-7"}} if registered else {}
    gateway._filtered_call_signs = ["KQ4WAA-7"] if registered else []
    sent_to_mesh = []
    gateway._send_mesh_message = lambda *args: sent_to_mesh.append(args)

    gateway._process_meshcore_packet(
        {
            "fromId": sender_id,
            "toId": gateway._gateway_id,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": b"?",
            },
        }
    )

    assert len(sent_to_mesh) == 1
    _, reply = sent_to_mesh[0]
    assert len(reply.encode("utf-8")) <= 160


@pytest.mark.parametrize("message", ["aprs?", "APRS?", " AprS? "])
def test_aprs_query_sends_private_online_reply_within_meshcore_limit(message):
    sender_id = "!abcdef012345"
    gateway = Gateway.__new__(Gateway)
    gateway._gateway_id = "!123456789abc"
    gateway._registry = {}
    gateway._filtered_call_signs = []
    sent_to_mesh = []
    gateway._send_mesh_message = lambda *args: sent_to_mesh.append(args)

    gateway._process_meshcore_packet(
        {
            "fromId": sender_id,
            "toId": gateway._gateway_id,
            "decoded": {
                "portnum": "TEXT_MESSAGE_APP",
                "payload": message.encode("utf-8"),
            },
        }
    )

    assert sent_to_mesh == [
        (
            sender_id,
            'MC-APRS Gateway is online. Welcome! Reply "?" for more info.',
        )
    ]
    assert len(sent_to_mesh[0][1].encode("utf-8")) <= 160


@pytest.mark.parametrize(
    "message",
    [
        "aprs?",
        "APRS?",
        " AprS? ",
        "Aprs?\x00",
        "\x00Aprs?\x00 ",
        " aprs?\r\n",
        "KQ4WAA-MAMBO: aprs?",
    ],
)
def test_channel_zero_aprs_query_replies_publicly_and_advertises(message):
    class Interface:
        def __init__(self):
            self.actions = []

        def sendChannelText(self, text, channelIndex):
            self.actions.append(("channel", channelIndex, text))

        def sendFloodAdvert(self):
            self.actions.append(("advert",))

    gateway = Gateway.__new__(Gateway)
    gateway._gateway_id = "!123456789abc"
    gateway._registry = {}
    gateway._filtered_call_signs = []
    gateway._interface = Interface()

    gateway._process_meshcore_packet(
        {
            "decoded": {
                "portnum": "MESHCORE_CHANNEL_TEXT_MESSAGE",
                "channel_idx": 0,
                "payload": message.encode("utf-8"),
            }
        }
    )

    assert gateway._interface.actions == [
        (
            "channel",
            0,
            'MC-APRS Gateway online. Send a DM to this node with "?" for help.',
        ),
        ("advert",),
    ]
    assert len(gateway._interface.actions[0][2].encode("utf-8")) <= 160
