DEFAULT_CONFIG_CONTENT = """# 
# MC-APRS CONFIGURATION FILE (version: 1)
# Be sure to at least modify 'call_sign' and 'aprsis_passcode'.
#


# Radio call sign of the gateway itself (analogy, iGate's call sign)
call_sign: N0CALL 


# APRS-IS passcode. Search Google for how to get this
aprsis_passcode: 12345 


# Maximum length of an APRS text message. 
# If null, or commented out, default to 67 as per APRS specification
# max_aprs_message_length: 128


# MeshCore Companion serial connection
meshcore_interface:
  type: serial
  device: /dev/ttyACM0
  baudrate: 115200
  # Send a Flood Advert on startup, then every 12 hours
  flood_advert: false


# Beacon new registrations to APRS-IS to facilitate discovery
beacon_registrations: true 


# Should the gateway beacon its own position
gateway_beacon:
  enabled: true
  icon: "OGM"                 # 'M' in a diamond, representing a Gateway
#  latitude: 47.6205063       # Enter gateway coordinates manually for APRS beaconing
#  longitude: -122.3518523


# Where should logs be stored?
# If null, (or commented out), store logs in the `logs` dir, sibling to this file. 
#logs_dir: null


# Where should data be stored?
# If null, (or commented out), store data in the `data` dir, sibling to this file. 
#data_dir: null
"""
