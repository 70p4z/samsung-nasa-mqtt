import packetgateway
import os
import tools
import logging
import time

LOGLEVEL = os.environ.get('LOGLEVEL', 'INFO').upper()
LOGFORMAT = '%(asctime)s %(levelname)s %(threadName)s %(message)s'
logging.basicConfig(format=LOGFORMAT)
log = logging.getLogger("sample")
log.setLevel(LOGLEVEL)


def rx_event_nasa(p):
  log.debug("packet received "+ tools.bin2hex(p))
  parser.parse_nasa(p)

pgw = packetgateway.PacketGateway("127.0.0.1", 7001, rx_event=rx_event_nasa)
parser = packetgateway.NasaPacketParser()

pgw.start()

packetNumber = 1
while True:

	time.sleep(10)

	# request DHW OFF
	pgw.packet_tx(tools.hex2bin(f"520000b0ff20c013f70340650040660142350226"))
	packetNumber += 1

	time.sleep(10)
	sys.exit(-1)
