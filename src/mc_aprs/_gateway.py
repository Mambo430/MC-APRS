import time
import logging
import warnings
import random
import re
import traceback
from queue import Queue, Empty
from .__about__ import __version__
from ._aprs_client import APRSClient
from ._aprs_symbols import get_symbol_code
from ._meshcore_interface import MeshCoreInterface
from ._registry import CallSignRegistry

logger = logging.getLogger("mc_aprs")

MAX_APRS_TEXT_MESSAGE_LENGTH = 67

APRS_SOFTWARE_ID = "APZMCG"  # Experimental MeshCore-APRS Gateway
REGISTRATION_BEACON = "MESHID-01"
GATEWAY_BEACON_INTERVAL = 3600  # Station beacons once an hour

SERIAL_WATCHDOG_INTERVAL = 60  # Check the serial state every minute

# Beacons that mean unregister
APRS_TOMBSTONE = "N0NE-01"
MESH_TOMBSTONE = "!000000000000"

# Icons
DEFAULT_GATEWAY_ICON = "OGM"
DEFAULT_GATEWAY_SYMBOL = "M&"

# For uptime
TIME_DURATION_UNITS = (
    ("w", 60 * 60 * 24 * 7),
    ("d", 60 * 60 * 24),
    ("h", 60 * 60),
    ("m", 60),
    ("s", 1),
)


class Gateway(object):
    def __init__(self, config):
        self._config = config
        self._start_time = None

        self._gateway_id = None
        self._gateway_call_sign = None

        self._interface = None
        self._mesh_rx_queue = Queue()

        self._aprs_client = None
        self._max_aprs_message_length = config.get("max_aprs_message_length")
        if self._max_aprs_message_length is None:
            self._max_aprs_message_length = MAX_APRS_TEXT_MESSAGE_LENGTH

        self._reply_to = {}
        self._filtered_call_signs = []
        self._beacon_registrations = False

        self._next_beacon_time = 0
        self._next_serial_check_time = 0

        self._registry = CallSignRegistry(config.get("data_dir"))

    def run(self):
        # For measuring uptime
        self._start_time = time.time()

        # Connect to the MeshCore Companion
        interface_config = self._config.get("meshcore_interface", {})

        def on_recv(packet, interface=None):
            self._mesh_rx_queue.put(packet)

        self._interface = self._get_interface(interface_config, on_recv)
        node_info = self._interface.getMyNodeInfo()
        self._gateway_id = node_info.get("user", {}).get("id")
        logger.debug(f"Gateway device id: {self._gateway_id}")

        # Myself
        self._gateway_call_sign = self._config.get("call_sign", "").upper().strip()
        self._filtered_call_signs.append(self._gateway_call_sign)

        # The registraion beacon
        self._beacon_registrations = self._config.get("beacon_registrations", True)
        if self._beacon_registrations:
            self._filtered_call_signs.append(REGISTRATION_BEACON)

        for contact in self._interface.getContacts():
            public_key = contact.get("public_key", "").lower()
            if len(public_key) < 12:
                continue
            presumptive_id = "!" + public_key[:12]
            if presumptive_id not in self._registry:
                continue

            last_heard = contact.get("last_advert")
            if last_heard is None or last_heard + 3600 * 24 < time.time():
                continue

            call_sign = self._registry[presumptive_id]["call_sign"]
            if call_sign not in self._filtered_call_signs:
                self._filtered_call_signs.append(call_sign)

        # Connect to APRS IS
        aprsis_passcode = self._config.get("aprsis_passcode")
        self._aprs_client = APRSClient(
            self._gateway_call_sign,
            aprsis_passcode,
            "g/" + "/".join(self._filtered_call_signs),
        )

        logger.debug("Pausing for 2 seconds...")
        time.sleep(2.0)
        logger.debug("Starting main loop.")

        gateway_beacon = self._config.get("gateway_beacon", {})
        if gateway_beacon.get("enabled") and (
            gateway_beacon.get("latitude") is None
            or gateway_beacon.get("longitude") is None
        ):
            logger.warning(
                "Gateway APRS beaconing is enabled but coordinates are missing; "
                "set gateway_beacon.latitude and gateway_beacon.longitude."
            )

        while True:
            # There are four independent steps performed by this loop: servicing watchdogs,
            # beaconing, reading from MeshCore, and reading from APRS.
            # Make sure that errors in one don't stop the others.
            now = time.time()

            # 1. Service the watchdogs
            ############################
            # MeshCore's client handles serial reconnection. Do not treat an idle
            # mesh as a failed connection; Companion nodes can be quiet for long periods.
            if now > self._next_serial_check_time:
                self._next_serial_check_time = now + SERIAL_WATCHDOG_INTERVAL
                if not self._interface.is_connected:
                    logger.warning(
                        "MeshCore serial connection is disconnected; reconnect will be attempted."
                    )

            # 2. Beacon the gateway position
            ################################
            try:
                if now > self._next_beacon_time and gateway_beacon.get("enabled"):
                    gate_lat = gateway_beacon.get("latitude")
                    gate_lon = gateway_beacon.get("longitude")

                    # MeshCore gateway coordinates are configured explicitly.
                    if gate_lat is None or gate_lon is None:
                        self._next_beacon_time = now + 60
                    else:
                        self._send_aprs_gateway_beacon(
                            gate_lat,
                            gate_lon,
                            gateway_beacon.get("icon", DEFAULT_GATEWAY_ICON),
                            "MC-APRS: " + self._gateway_id,
                        )
                        self._next_beacon_time = now + GATEWAY_BEACON_INTERVAL
            except Exception as e:
                logger.error(traceback.format_exc())

            # 3. Read a MeshCore packet
            ############################
            try:
                mesh_packet = None
                try:
                    mesh_packet = self._mesh_rx_queue.get(block=False)
                except Empty:
                    pass

                if mesh_packet is not None:
                    self._process_meshcore_packet(mesh_packet)
            except Exception as e:
                logger.error(traceback.format_exc())

            # 4. Read an APRS packet
            ########################
            try:
                aprs_packet = self._aprs_client.recv()
                if aprs_packet is not None:
                    self._process_aprs_packet(aprs_packet)
            except Exception as e:
                logger.error(traceback.format_exc())

            # Yield
            time.sleep(0.001)

    def _get_interface(self, interface_config, on_message):
        connection_type = interface_config.get("type", "serial")
        baudrate = interface_config.get("baudrate", 115200)
        flood_advert = interface_config.get("flood_advert", False)
        port = None

        if connection_type == "serial":
            endpoint = interface_config.get("device")
            if not endpoint:
                raise ValueError(
                    "Set 'meshcore_interface.device' to the Companion serial port "
                    "in 'mc-aprs.yaml'."
                )
        elif connection_type == "tcp":
            endpoint = interface_config.get("host")
            port = interface_config.get("port")
            if not endpoint:
                raise ValueError(
                    "Set 'meshcore_interface.host' to the MeshCore TCP host in "
                    "'mc-aprs.yaml'."
                )
            if (
                not isinstance(port, int)
                or isinstance(port, bool)
                or not 1 <= port <= 65535
            ):
                raise ValueError(
                    "Set 'meshcore_interface.port' to a TCP port from 1 to 65535 "
                    "in 'mc-aprs.yaml'."
                )
        else:
            raise ValueError(
                "Set 'meshcore_interface.type' to either 'serial' or 'tcp' "
                "in 'mc-aprs.yaml'."
            )

        return MeshCoreInterface(
            endpoint,
            baudrate,
            on_message,
            flood_advert=flood_advert,
            connection_type=connection_type,
            port=port,
        ).connect()

    def close(self):
        if self._interface is not None:
            self._interface.close()

    def _process_meshcore_packet(self, packet):
        fromId = packet.get("fromId", None)
        toId = packet.get("toId", None)
        portnum = packet.get("decoded", {}).get("portnum")

        # Don't bother logging my telemetry
        if portnum != "TELEMETRY_APP" or fromId != self._gateway_id:
            logger.info(f"{fromId} -> {toId}: {portnum}")

        # Record that we have spotted the ID
        should_announce = self._spotted(fromId)

        if portnum == "TEXT_MESSAGE_APP":
            message_bytes = packet["decoded"]["payload"]
            message_string = message_bytes.decode("utf-8")

            if toId != self._gateway_id:
                # Not for me
                return

            if message_string.strip().casefold() == "aprs?":
                self._send_mesh_message(
                    fromId,
                    'MC-APRS Gateway is online. Welcome! Reply "?" for more info.',
                )
                return

            if message_string.strip() == "?":
                # Different call signs for registered and non-registered devices
                if fromId not in self._registry:
                    self._send_mesh_message(
                        fromId,
                        "Register: !register CALLSIGN-SSID (e.g. !register N0CALL-1). "
                        "Licensed operators only. Send APRS as DEST-CALL: message. "
                        "See MC-APRS docs.",
                    )
                    return
                else:
                    self._send_mesh_message(
                        fromId,
                        "Send APRS as DEST-CALL: message (e.g. N0CALL-2: hello). "
                        "Replies may omit the prefix.",
                    )
                    return

            if message_string.strip() == "!id" or message_string.strip() == "!version":
                # Let clients query the gateway call sign and version number
                self._send_mesh_message(
                    fromId,
                    f"Gateway call sign: {self._gateway_call_sign}, Uptime: {self._uptime()}, Version: {__version__}",
                )
                return

            if message_string.lower().strip().startswith("!register"):
                # Allow operatores to join
                m = re.search(
                    r"^!register:?\s+([a-z0-9]{4,7}\-[0-9]{1,2})$",
                    message_string.lower().strip(),
                )
                if m:
                    call_sign = m.group(1).upper()
                    icon = None

                    # Update the database
                    if fromId in self._registry:
                        # Update
                        self._registry.add_registration(fromId, call_sign, icon, True)
                        self._send_mesh_message(fromId, "Registration updated.")
                    else:
                        # New
                        self._registry.add_registration(fromId, call_sign, icon, True)
                        self._spotted(fromId)
                        self._send_mesh_message(
                            fromId,
                            "Registered. Send APRS messages by replying here and prefixing your message with the destination callsign. Example: 'DEST-CALL: hello'.",
                        )
                    self._spotted(fromId)  # Run this again to update subscriptions

                    # Beacon the registration to APRS-IS to facilitate building a shared roaming mapping
                    if self._beacon_registrations:
                        self._send_registration_beacon(fromId, call_sign, icon)
                else:
                    self._send_mesh_message(
                        fromId,
                        "Invalid call sign.\nSYNTAX: !register [CALLSIGN]-[SSID]\nE.g.,\n!register N0CALL-1",
                    )
                return

            if message_string.lower().strip().startswith("!unregister"):
                if fromId not in self._registry:
                    self._send_mesh_message(
                        fromId, "Device is not registered. Nothing to do."
                    )
                    return
                call_sign = self._registry[fromId]["call_sign"]
                self._registry.add_registration(fromId, None, None, True)
                self._registry.add_registration(None, call_sign, None, True)

                if self._beacon_registrations:
                    self._send_registration_beacon(fromId, APRS_TOMBSTONE, None)
                    self._send_registration_beacon(MESH_TOMBSTONE, call_sign, None)

                if call_sign in self._filtered_call_signs:
                    self._filtered_call_signs.remove(call_sign)

                self._aprs_client.set_filter("g/" + "/".join(self._filtered_call_signs))
                self._send_mesh_message(fromId, "Device unregistered.")
                return

            if fromId not in self._registry:
                self._send_mesh_message(
                    fromId,
                    "Unknown device. HAM license required!\nRegister by replying with:\n!register [CALLSIGN]-[SSID]\nE.g.,\n!register N0CALL-1",
                )
                return

            m = re.search(r"^([A-Za-z0-9]+(\-[A-Za-z0-9]+)?):(.*)$", message_string)
            if m:
                tocall = m.group(1)
                self._reply_to[fromId] = tocall
                message = m.group(3).strip()
                self._send_aprs_message(
                    self._registry[fromId]["call_sign"], tocall, message
                )
                return
            elif fromId in self._reply_to:
                self._send_aprs_message(
                    self._registry[fromId]["call_sign"],
                    self._reply_to[fromId],
                    message_string,
                )
                return
            else:
                self._send_mesh_message(
                    fromId,
                    "Please prefix your message with the destination callsign. Example: 'DEST-CALL: hello'.",
                )
                return

        if portnum == "MESHCORE_CHANNEL_TEXT_MESSAGE":
            message_string = packet["decoded"]["payload"].decode("utf-8")
            channel_text = message_string.rsplit(":", 1)[-1].strip("\x00 \t\r\n")
            if (
                packet["decoded"].get("channel_idx") == 0
                and channel_text.casefold() == "aprs?"
            ):
                logger.info("Received Public channel online query (aprs?).")
                self._interface.sendChannelText(
                    'MC-APRS Gateway online. Send a DM to this node with "?" for help.',
                    channelIndex=0,
                )
                self._interface.sendFloodAdvert()
            return

        # At this point the message was not handled yet. Announce yourself
        if should_announce:
            self._send_mesh_message(
                fromId,
                "APRS-tastic Gateway available here. Welcome. Reply '?' for more info.",
            )

    def _process_aprs_packet(self, packet):
        if packet.get("format") == "message":
            fromcall = packet.get("from", "N0CALL").strip().upper()
            tocall = packet.get("addresse", "").strip().upper()

            # Is this an ack?
            if packet.get("response") == "ack":
                logger.debug(
                    f"Received ACK to {tocall}'s message #" + packet.get("msgNo", "")
                )
                return

            # Is this a registration beacon?
            if tocall == REGISTRATION_BEACON:
                mesh_id = packet.get("message_text", "").lower().strip()
                m = re.search(
                    r"^(!(?:[a-f0-9]{8}|[a-f0-9]{12}))"
                    r"(?::(\$\$|[A-Za-z0-9]{2,3}))?$",
                    mesh_id,
                )
                if m:
                    mesh_id = m.group(1)
                    icon = m.group(2)

                    # Validate the icon
                    if icon is None:
                        pass
                    elif icon == "":
                        icon = None
                    elif icon != "$$":
                        icon = icon.upper()
                        symbol = get_symbol_code(icon)
                        if symbol is None:
                            icon = None

                    # Handle tombstones
                    if mesh_id in (MESH_TOMBSTONE, "!00000000"):
                        mesh_id = None
                    if fromcall == APRS_TOMBSTONE:
                        fromcall = None

                    logger.info(
                        f"Observed registration beacon: {mesh_id}: {fromcall}, icon: {icon}",
                    )
                    self._registry.add_registration(mesh_id, fromcall, icon, False)
                else:
                    # Not necessarily and error. Could be from a future version
                    logger.debug(
                        f"Unknown registration beacon: {packet.get('raw')}",
                    )
                return

            # Ack all remaining messages (which aren't themselves acks, and aren't beacons)
            self._send_aprs_ack(tocall, fromcall, packet.get("msgNo", ""))

            # Message was sent to the gateway itself. Respond with station information.
            if tocall == self._gateway_call_sign:
                self._send_aprs_message(
                    tocall,
                    fromcall,
                    f"Gateway ID: {self._gateway_id}, Uptime: {self._uptime()}, Version: {__version__}",
                )
                return

            # Figure out where the packet is going
            toId = None
            for k in self._registry:
                if tocall == self._registry[k]["call_sign"].strip().upper():
                    toId = k
                    break
            if toId is None:
                logger.error(f"Unkown recipient: {tocall}")
                return

            # Forward the message
            message = packet.get("message_text")
            if message is not None:
                self._reply_to[toId] = fromcall
                self._send_mesh_message(toId, fromcall + ": " + message)

    def _send_aprs_message(self, fromcall, tocall, message):
        message_chunks = self._chunk_message(message, self._max_aprs_message_length)

        while len(tocall) < 9:
            tocall += " "

        for chunk in message_chunks:
            packet = (
                fromcall
                + ">"
                + APRS_SOFTWARE_ID
                + ",WIDE1-1,qAR,"
                + self._gateway_call_sign
                + "::"
                + tocall
                + ":"
                + chunk.strip()
                + "{"
                + str(random.randint(0, 999))
            )
            logger.debug("Sending to APRS: " + packet)
            self._aprs_client.send(packet)

    def _send_aprs_ack(self, fromcall, tocall, messageId):
        while len(tocall) < 9:
            tocall += " "
        packet = (
            fromcall
            + ">"
            + APRS_SOFTWARE_ID
            + ",WIDE1-1,qAR,"
            + self._gateway_call_sign
            + "::"
            + tocall
            + ":ack"
            + messageId
        )
        logger.debug("Sending to APRS: " + packet)
        self._aprs_client.send(packet)

    def _aprs_lat(self, lat):
        aprs_lat_ns = "N" if lat >= 0 else "S"
        lat = abs(lat)
        aprs_lat_deg = int(lat)
        aprs_lat_min = float((lat - aprs_lat_deg) * 60)
        return f"%02d%05.2f%s" % (aprs_lat_deg, aprs_lat_min, aprs_lat_ns)

    def _aprs_lon(self, lon):
        aprs_lon_ew = "E" if lon >= 0 else "W"
        lon = abs(lon)
        aprs_lon_deg = abs(int(lon))
        aprs_lon_min = float((lon - aprs_lon_deg) * 60)
        return f"%03d%05.2f%s" % (aprs_lon_deg, aprs_lon_min, aprs_lon_ew)

    def _send_aprs_gateway_beacon(self, lat, lon, icon, message):
        aprs_lat = self._aprs_lat(lat)
        aprs_lon = self._aprs_lon(lon)

        # Convert the icon to a symbol
        symbol = get_symbol_code(icon)
        if symbol is None:
            symbol = DEFAULT_GATEWAY_SYMBOL

        aprs_msg = "!" + aprs_lat + symbol[0] + aprs_lon + symbol[1] + message
        packet = (
            self._gateway_call_sign + ">" + APRS_SOFTWARE_ID + ",TCPIP*:" + aprs_msg
        )
        logger.debug(f"Beaconing to APRS: {packet}")
        self._aprs_client.send(packet)

    def _send_mesh_message(self, destid, message):
        logger.info(f"Sending to '{destid}': {message}")
        self._interface.sendText(
            text=message, destinationId=destid, wantAck=True, wantResponse=False
        )

    def _spotted(self, node_id):
        """
        Checks if the node is registered, and newly spotted -- in which
        case we need to update the APRS filters, and also greet the user.
        In this case, it returns true. Otherwise, false.
        """

        # We spotted them, but they aren't registered
        if node_id not in self._registry:
            return False

        call_sign = self._registry[node_id]["call_sign"]

        if call_sign in self._filtered_call_signs:
            return False

        # Ok it's new, update the filters
        self._filtered_call_signs.append(call_sign)
        self._aprs_client.set_filter("g/" + "/".join(self._filtered_call_signs))
        return True

    def _uptime(self):
        if self._start_time is None:
            return "None"

        seconds = int(time.time() - self._start_time)

        if seconds < 1:
            return "0s"

        parts = []
        for unit, div in TIME_DURATION_UNITS:
            amount, seconds = divmod(int(seconds), div)
            if amount > 0:
                parts.append("{}{}".format(amount, unit))
            if len(parts) >= 3:  # Don't get overly precise
                break

        return ", ".join(parts)

    def _truncate_message(self, message, max_bytes):
        m_len = len(message.encode("utf-8"))

        # Nothing to do
        if m_len <= max_bytes:
            return message

        # Warn about the message being too long
        warnings.warn(
            f"Message of length {m_len} bytes exceeds the protocol maximum of {max_bytes} bytes."
        )

        # Trim first by characters to get close, then remove one character at a time
        strlen = max_bytes
        message = message[0:strlen]
        while len(message.encode("utf-8")) > max_bytes:
            # Chop a character
            strlen -= 1
            message = message[0:strlen]
        return message

    def _chunk_message(self, message, max_bytes):
        chunks = list()

        # We want to break on spaces
        tokens = re.split(r"(\s+)", message)
        if len(tokens) == 0:
            return chunks

        buffer = tokens.pop(0)  # Always include at least one token
        while len(tokens) > 0:
            token = tokens.pop(0)
            if len((buffer + token).encode("utf-8")) > max_bytes:
                chunks.append(buffer)
                buffer = token
            else:
                buffer += token
        if len(buffer) > 0:
            chunks.append(buffer)
            buffer = ""

        return chunks

    def _send_registration_beacon(self, device_id, call_sign, icon):
        logger.info(
            f"Beaconing registration {call_sign} <-> {device_id} (icon: {icon}), to {REGISTRATION_BEACON}"
        )
        if icon is None:
            self._send_aprs_message(call_sign, REGISTRATION_BEACON, device_id)
        else:
            self._send_aprs_message(
                call_sign, REGISTRATION_BEACON, device_id + ":" + icon
            )
