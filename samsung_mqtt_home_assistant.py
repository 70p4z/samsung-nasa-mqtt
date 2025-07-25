import packetgateway
import os
import tools
import time
import threading 
import argparse
import traceback
import paho.mqtt.client as mqtt
import json
import sys
import signal
from datetime import datetime, timedelta

from nasa_messages import *

from logger import log

def auto_int(x):
  return int(x, 0)

parser = argparse.ArgumentParser()
parser.add_argument('--mqtt-host', default="192.168.0.4", help="host to connect to the MQTT broker")
parser.add_argument('--mqtt-port', default="1883", type=auto_int, help="port of the MQTT broker")
parser.add_argument('--serial-host', default="127.0.0.1",help="host to connect the serial interface endpoint (i.e. socat /dev/ttyUSB0,parenb,raw,echo=0,b9600,nonblock,min=0 tcp-listen:7001,reuseaddr,fork )")
parser.add_argument('--serial-port', default="7001", type=auto_int, help="port to connect the serial interface endpoint")
parser.add_argument('--nasa-interval', default="30", type=auto_int, help="Interval in seconds to republish MQTT values set from the MQTT side (useful for temperature mainly)")
parser.add_argument('--nasa-timeout', default="120", type=auto_int, help="Timeout before considering communication fault")
parser.add_argument('--dump-only', action="store_true", help="Request to only dump packets from the NASA link on the console")
parser.add_argument('--nasa-addr', default="510000", help="Configurable self address to use")
parser.add_argument('--nasa-pnp', action="store_true", help="Perform Plug and Play when set")
parser.add_argument('--nasa-default-zone-temp', help="Set given default temperature when MQTT restart or communication is lost or when PNP is timeout", type=auto_int)
args = parser.parse_args()

# NASA state
nasa_state = {}
nasa_fsv_unlocked = False
mqtt_client = None
mqtt_published_vars = {}
pgw = None
last_nasa_rx = time.time()
NASA_PNP_TIMEOUT=30
NASA_PNP_CHECK_INTERVAL=30
NASA_PNP_CHECK_RETRIES=10 # avoid fault on PNP to avoid temp to be messed up and the ASHP to stall
NASA_PNP_RESPONSE_TIMEOUT=10
nasa_pnp_time=0
nasa_pnp_check_retries=0
nasa_pnp_ended=False
nasa_pnp_check_requested=False
desynch=0
nasa_update_timeout_checks = []

class NASAUpdateTimeoutCheck():
  def __init__ (self, updatecommand, _msgnum, expectedintvalue, timeout_s=0):
    self._msgnum = _msgnum
    self.updatecommand = updatecommand
    self.expectedintvalue = expectedintvalue
    self.timeout_time = 0
    self.timeout_s = timeout_s
    self.reset_timeout()

  def timeout(self):
    to = self.timeout_s > 0 and time.time() > self.timeout_time
    log.debug("NASA update timeout " + hex(self._msgnum) + ": " + str(to))
    return to 

  def reset_timeout(self):
    if self.timeout_s > 0:
      self.timeout_time = time.time() + self.timeout_s

  def check(self):
    global nasa_state
    nasa_name = nasa_message_name(self._msgnum)
    if not nasa_name in nasa_state:
      log.debug("NASA check " + hex(self._msgnum) + " not present")
      return False
    log.debug("NASA check " + hex(self._msgnum) + ": " + hex(nasa_state[nasa_name]) + " =?= " + hex(self.expectedintvalue) )
    return nasa_state[nasa_name] == self.expectedintvalue

  def msgnum(self):
    return self._msgnum

  def command(self):
    return self.updatecommand

def nasa_write_with_check_command(command, msgnum, expectedintvalue, timeout_s=5.0):
  # avoid multiple commands with same target message num, only keep the last
  for nutc in nasa_update_timeout_checks:
    if nutc.msgnum() == msgnum:
      nasa_update_timeout_checks.remove(nutc)
      break

  # send command over the bus
  pgw.packet_tx(command)
  # remove the value from the nasa cache
  if nasa_message_name(msgnum) in nasa_state:
    del nasa_state[nasa_message_name(msgnum)]
  # schedule a check to watch the value if it evolved
  nasa_update_timeout_checks.append(NASAUpdateTimeoutCheck(command, msgnum, expectedintvalue, timeout_s))

def nasa_update(msgnum, intval):
  try:
    pub=False
    nasa_name = nasa_message_name(msgnum)
    if not nasa_name in nasa_state:
      pub=True
    else:
      if nasa_state[nasa_name] != intval:
        pub=True
    nasa_state[nasa_name] = intval
    return pub
  except BaseException as e:
    log.error(e, exc_info=True)
  return False

def nasa_reset_state():
  global nasa_state
  nasa_state = {}
  log.info("reset NASA state")
  # default ambient values to avoid heating (0xC8 by default)
  if args.nasa_default_zone_temp:
    log.info("set default zone 1/2 current temperature to: " + str(args.nasa_default_zone_temp) )
    nasa_state[nasa_message_name(0x423A)] = args.nasa_default_zone_temp*10
    nasa_state[nasa_message_name(0x42DA)] = args.nasa_default_zone_temp*10


def nasa_payload_mqtt_handler(client, userdata, msg):
  global pgw
  mqttpayload = msg.payload.decode('utf-8')
  try:
    binpayload = tools.hex2bin(mqttpayload)
    mqtt_client.publish('homeassistant/text/samsung_ehs_payload/state', mqttpayload, retain=True)
    # will prepend start, compute and append crc and stop
    pgw.packet_tx(binpayload)
  except BaseException as e:
    log.error(e, exc_info=True)

def nasa_fsv_writable():
  global nasa_fsv_unlocked
  return nasa_fsv_unlocked

def nasa_fsv_unlock_mqtt_handler(client, userdata, msg):
  global nasa_fsv_unlocked
  mqttpayload = msg.payload.decode('utf-8')
  if mqttpayload == "ON":
    mqtt_client.publish('homeassistant/switch/samsung_ehs_fsv_unlock/state', 'ON', retain=True)
    nasa_fsv_unlocked=True
  else:
    mqtt_client.publish('homeassistant/switch/samsung_ehs_fsv_unlock/state', 'OFF', retain=True)
    nasa_fsv_unlocked=False

class MQTTHandler():
  def __init__(self, mqtt_client, topic, nasa_msgnum):
    self.topic = topic
    self.nasa_msgnum = nasa_msgnum
    self.mqtt_client = mqtt_client

  def publish(self, valueInt):
    log.info("default nasa handler for 0x" + hex(self.nasa_msgnum))
    self.mqtt_client.publish(self.topic, valueInt, retain=True)

  def action(self, client, userdata, msg):
    log.info("default mqtt handler for 0x" + hex(self.nasa_msgnum))
    pass

  def initread(self):
    pass

  def can_modify(self):
    return True

class WriteMQTTHandler(MQTTHandler):
  def __init__(self, mqtt_client, topic, nasa_msgnum, multiplier=1):
    super().__init__(mqtt_client, topic, nasa_msgnum)
    self.multiplier = float(multiplier)
  def publish(self, valueInt):
    if self.multiplier > 1:
      self.mqtt_client.publish(self.topic, valueInt/self.multiplier, retain=True)
    else:
      self.mqtt_client.publish(self.topic, valueInt, retain=True)
  def action(self, client, userdata, msg):
    mqttpayload = msg.payload.decode('utf-8')
    log.info(self.topic + " = " + mqttpayload)
    if not self.can_modify():
      self.mqtt_client.publish(self.topic, nasa_state[nasa_message_name(self.nasa_msgnum)], retain=True)
      return
    intval = int(float(msg.payload.decode('utf-8'))*self.multiplier)
    if nasa_update(self.nasa_msgnum, intval) or True:
      nasa_write_with_check_command(nasa_write(self.nasa_msgnum, intval), self.nasa_msgnum, intval)
  def initread(self):
    global pgw
    pgw.packet_tx(nasa_read(self.nasa_msgnum))

class FSVWriteMQTTHandler(WriteMQTTHandler):
  def can_modify(self):
    return nasa_fsv_writable()

class FSVONOFFMQTTHandler(FSVWriteMQTTHandler):
  def publish(self, valueInt):
    valueStr = "ON"
    if valueInt==0:
      valueStr="OFF"
    self.mqtt_client.publish(self.topic, valueStr, retain=True)
  def action(self, client, userdata, msg):
    mqttpayload = msg.payload.decode('utf-8')
    log.info(self.topic + " = " + mqttpayload)
    if not self.can_modify():
      self.mqtt_client.publish(self.topic, nasa_state[nasa_message_name(self.nasa_msgnum)], retain=True)
      return
    intval=0
    if mqttpayload == "ON":
      intval=1
    if nasa_update(self.nasa_msgnum, intval) or True:
      nasa_write_with_check_command(nasa_write(self.nasa_msgnum, intval), self.nasa_msgnum, intval)

class SetMQTTHandler(WriteMQTTHandler):
  def action(self, client, userdata, msg):
    mqttpayload = msg.payload.decode('utf-8')
    log.info(self.topic + " = " + mqttpayload)
    if not self.can_modify():
      self.mqtt_client.publish(self.topic, nasa_state[nasa_message_name(self.nasa_msgnum)], retain=True)
      return
    intval = int(float(msg.payload.decode('utf-8'))*self.multiplier)
    if nasa_update(self.nasa_msgnum, intval) or True:
      global pgw
      nasa_write_with_check_command(nasa_set(self.nasa_msgnum, intval), self.nasa_msgnum, intval)

class FSVSetMQTTHandler(SetMQTTHandler):
  def can_modify(self):
    return nasa_fsv_writable()

class StringIntMQTTHandler(WriteMQTTHandler):
  def __init__(self, mqtt_client, topic, nasa_msgnum, handler_parameter):
    super().__init__(mqtt_client, topic, nasa_msgnum)
    self.map = handler_parameter

  def publish(self, valueInt):
    for s in self.map:
      if self.map[s] == valueInt:
        self.mqtt_client.publish(self.topic, s, retain=True)  
        break
    else:
      self.mqtt_client.publish(self.topic, "Unknown ("+str(valueInt)+")", retain=True)

  def action(self, client, userdata, msg):
    mqttpayload = msg.payload.decode('utf-8')
    log.info(self.topic + " = " + mqttpayload)
    if not nasa_fsv_writable():
      self.publish(nasa_state[nasa_message_name(self.nasa_msgnum)])
      return
    global pgw
    payload = msg.payload.decode('utf-8')
    for s in self.map:
      if s == payload:
        valueInt = self.map[s]
        if nasa_update(self.nasa_msgnum, valueInt) or True:
          nasa_write_with_check_command(nasa_write(self.nasa_msgnum, valueInt), self.nasa_msgnum, valueInt)
          break
    else:
      log.error("ignoring '" +self.topic + "' value: '" + payload + "'")

class FSVStringIntMQTTHandler(StringIntMQTTHandler):
  def can_modify(self):
    return nasa_fsv_writable()

class ONOFFSetMQTTHandler(SetMQTTHandler):
  def publish(self, valueInt):
    valueStr = "ON"
    if valueInt==0:
      valueStr="OFF"
    self.mqtt_client.publish(self.topic, valueStr, retain=True)


class DHWONOFFMQTTHandler(ONOFFSetMQTTHandler):
  def action(self, client, userdata, msg):
    mqttpayload = msg.payload.decode('utf-8')
    log.info(self.topic + " = " + mqttpayload)
    intval=0
    if mqttpayload == "ON":
      intval=1
    if nasa_update(self.nasa_msgnum, intval) or True:
      global pgw
      nasa_write_with_check_command(nasa_dhw_power(intval == 1), 0x4065, intval)

class COPMQTTHandler(MQTTHandler):
  def publish(self, valueInt):
    self.mqtt_client.publish(self.topic, valueInt, retain=True)
    # compute COP and publish the value as well
    # round at 2 digits
    try:
      if valueInt == 0:
        valueInt = 14
      self.mqtt_client.publish(self.topic + "_cop", int(nasa_state[nasa_message_name(0x4426)]*100 / valueInt)/100, retain=True)
    except:
      pass

class Zone1IntDiv10MQTTHandler(SetMQTTHandler):
  def __init__(self, mqtt_client, topic, nasa_msgnum):
    super().__init__(mqtt_client, topic, nasa_msgnum, 10)
  def action(self, client, userdata, msg):
    global nasa_state
    mqttpayload = msg.payload.decode('utf-8')
    log.info(self.topic + " = " + mqttpayload)
    self.mqtt_client.publish(self.topic, mqttpayload, retain=True)
    new_temp = int(float(mqttpayload)*10)
    if nasa_update(0x423A, new_temp) or True:
      nasa_write_with_check_command(nasa_set_zone1_temperature(float(mqttpayload)), 0x4203, new_temp)

class Zone1SwitchMQTTHandler(ONOFFSetMQTTHandler):
  def action(self, client, userdata, msg):
    global nasa_state
    mqttpayload = msg.payload.decode('utf-8')
    log.info(self.topic + " = " + mqttpayload)
    intval=0
    if mqttpayload == "ON":
      intval=1
    nasa_write_with_check_command(nasa_zone_power(intval==1,1), 0x4000, intval)

class Zone2IntDiv10MQTTHandler(SetMQTTHandler):
  def __init__(self, mqtt_client, topic, nasa_msgnum):
    super().__init__(mqtt_client, topic, nasa_msgnum, 10)
  def action(self, client, userdata, msg):
    global nasa_state
    mqttpayload = msg.payload.decode('utf-8')
    log.info(self.topic + " = " + mqttpayload)
    self.mqtt_client.publish(self.topic, mqttpayload, retain=True)
    new_temp = int(float(mqttpayload)*10)
    if nasa_update(0x42DA, new_temp) or True:
      nasa_write_with_check_command(nasa_set_zone2_temperature(float(mqttpayload)), 0x42D4, new_temp)
      
class Zone2SwitchMQTTHandler(ONOFFSetMQTTHandler):
  def action(self, client, userdata, msg):
    global nasa_state
    mqttpayload = msg.payload.decode('utf-8')
    log.info(self.topic + " = " + mqttpayload)
    global pgw
    enabled = mqttpayload == "ON"
    val = 0
    if enabled:
      val = 1
    nasa_write_with_check_command(nasa_zone_power(enabled,2), 0x411e, val)

#handler(source, dest, isInfo, protocolVersion, retryCounter, packetType, payloadType, packetNumber, dataSets)
def rx_nasa_handler(*nargs, **kwargs):
  global mqtt_client
  global last_nasa_rx
  global args
  global pgw
  global nasa_pnp_check_requested
  global nasa_pnp_ended
  global desynch
  global nasa_state
  last_nasa_rx = time.time()
  packetType = kwargs["packetType"]
  payloadType = kwargs["payloadType"]
  packetNumber = kwargs["packetNumber"]
  dataSets = kwargs["dataSets"]
  source = kwargs["source"]
  dest = kwargs["dest"]

  nasa_log_packet(log, source, dest, packetType, payloadType, packetNumber, dataSets)

  # ignore non normal packets
  if packetType != "normal":
    log.info("ignoring type of packet")
    return

  # only interpret values from the heatpump, ignore other controllers (especially for the E653 error on overriden zone)
  if source[0]&0xF0 != 0x20:
    log.info("ignoring packet from that source")
    return

  # ignore read requests
  if payloadType != "notification" and payloadType != "write" and payloadType != "response":
    log.info("ignoring packet instruction")
    return

  if args.dump_only:
    return

  if args.nasa_pnp:
    # check if PNP packet
    if not nasa_pnp_ended and nasa_is_pnp_phase0_network_address(source, dest, dataSets):
      pgw.packet_tx(nasa_pnp_phase1_request_address(args.nasa_addr))
    elif not nasa_pnp_ended and nasa_is_pnp_phase3_addressing(source, dest, packetNumber, dataSets):
      pgw.packet_tx(nasa_pnp_phase4_ack())
      return
    elif nasa_is_pnp_end(source, dest, dataSets):
      pgw.packet_tx(nasa_poke())
      nasa_pnp_ended = True
      nasa_pnp_check_requested=False
      return

  for ds in dataSets:
    try:
      # we can tag the master's address
      if ( ds[1] == "NASA_IM_MASTER_NOTIFY" and ds[4][0] == 1) or (ds[1] == "NASA_IM_MASTER" and ds[4][0] == 1):
        nasa_state["master_address"] = source

      # check if data is not in synch with cache
      if payloadType == "notification" and tools.bin2hex(source) == "200000":
        if ds[0] == 0x4203:
          # check if zone1 temp reported is the same as the last sent zone1 temp
          if nasa_message_name(0x423A) in nasa_state:
            if nasa_state[nasa_message_name(0x423A)] == ds[4][0]:
              # match => check is valid, no need to reperform PNP
              desynch=0
            else:
              desynch+=1
        if ds[0] == 0x42D4:
          # check if zone2 temp reported is the same as the last sent zone2 temp
          if nasa_message_name(0x42DA) in nasa_state:
            if nasa_state[nasa_message_name(0x42DA)] == ds[4][0]:
              # match => check is valid, no need to reperform PNP
              desynch=0
            else:
              desynch+=1

      # detect PNP check's response
      if args.nasa_pnp:
        if nasa_pnp_ended and nasa_pnp_check_requested and desynch == 0:
          nasa_pnp_check_requested=False

      # hold the value indexed by its name, for easier update of mqtt stuff
      # (set the int raw value)
      nasa_state[ds[1]] = ds[4][0]

      if ds[1] in mqtt_published_vars:
        # use the topic name and payload formatter from the mqtt publish array
        mqtt_p_v = mqtt_published_vars[ds[1]]
        mqtt_p_v.publish(ds[4][0])

      mqtt_client.publish('homeassistant/sensor/samsung_ehs/nasa_'+hex(ds[0]), payload=ds[2], retain=True)
    except BaseException as e:
      log.error(e, exc_info=True)

def rx_event_nasa(p):
  log.debug("packet received "+ tools.bin2hex(p))
  parser.parse_nasa(p, rx_nasa_handler)

#todo: make that parametrized
pgw = packetgateway.PacketGateway(args.serial_host, args.serial_port, rx_event=rx_event_nasa, rxonly=args.dump_only)
parser = packetgateway.NasaPacketParser()
pgw.start()
#ensure gateway is available and publish mqtt is possible when receving values
time.sleep(2)

# once in a while, publish zone2 current temp
def publisher_thread():
  global pgw
  global last_nasa_rx
  global nasa_pnp_time
  global nasa_pnp_check_retries
  global nasa_pnp_check_requested
  global nasa_pnp_ended
  global desynch
  # wait until IOs are setup
  time.sleep(10)
  nasa_last_publish = 0
  time_update_flow_target = 0

  if not args.nasa_pnp:
    nasa_set_attributed_address(args.nasa_addr)

  while True:
    log.debug("publisher iteration")
    try:
      # handle communication timeout
      if args.nasa_timeout > 0 and last_nasa_rx + args.nasa_timeout < time.time():
        log.info("Communication lost!")
        os.kill(os.getpid(), signal.SIGTERM)

      # wait until pnp is done before requesting values
      if nasa_last_publish + args.nasa_interval < time.time():
        if nasa_pnp_ended or not args.nasa_pnp:
          nasa_last_publish = time.time()
          pgw.packet_tx(nasa_notify_error(0))
          pgw.packet_tx(nasa_set_dhw_reference(0))
          # publish zone 1 and 2 values toward nasa (periodic keep alive)
          zone1_temp_name = nasa_message_name(0x423A) # don't use value for the EHS, but from sensors instead
          if zone1_temp_name in nasa_state:
            pgw.packet_tx(nasa_set_zone1_temperature(float(int(nasa_state[zone1_temp_name]))/10))
          zone2_temp_name = nasa_message_name(0x42DA) # don't use value for the EHS, but from sensors instead
          if zone2_temp_name in nasa_state:
            pgw.packet_tx(nasa_set_zone2_temperature(float(int(nasa_state[zone2_temp_name]))/10))

          for name in mqtt_published_vars:
            handler = mqtt_published_vars[name]
            if not nasa_message_name(handler.nasa_msgnum) in nasa_state and (isinstance(handler, FSVWriteMQTTHandler) or isinstance(handler, FSVSetMQTTHandler) or isinstance(handler, FSVStringIntMQTTHandler)):
              handler.initread()
              time.sleep(0.25)

      nasa_update_timeout_checks_rm=[]
      for nutc in nasa_update_timeout_checks:
        if nutc.timeout():
          # resend
          pgw.packet_tx(nutc.command())
          nutc.reset_timeout()
        if nutc.check():
          nasa_update_timeout_checks_rm.append(nutc)
      # avoid mod while iterated in previous loop
      for nutc in nasa_update_timeout_checks_rm:
        nasa_update_timeout_checks.remove(nutc)

      # update water flow target (each 10 seconds)
      if time.time() > time_update_flow_target:
        pgw.packet_tx(nasa_read([0x4202]))
        time_update_flow_target = time.time()+10

      #zone temps are not coherent with cached state
      if desynch >= 4:
        log.info("Too much desynch assuming communication is lost!")
        os.kill(os.getpid(), signal.SIGTERM)

      if args.nasa_pnp:
        # start PNP
        if not nasa_pnp_ended or (not nasa_pnp_ended and nasa_pnp_time + NASA_PNP_TIMEOUT < time.time()):
          pgw.packet_tx(nasa_pnp_phase0_request_network_address())
          nasa_pnp_time=time.time()
          nasa_pnp_ended=False
          nasa_pnp_check_requested=False
        # restart PNP?
        if nasa_pnp_ended and nasa_pnp_check_requested and nasa_pnp_time + NASA_PNP_RESPONSE_TIMEOUT < time.time():
          # retry check
          if nasa_pnp_check_retries < NASA_PNP_CHECK_RETRIES:
            pgw.packet_tx(nasa_read(0x4203))
            nasa_pnp_time=time.time()
            nasa_pnp_check_requested=True
          # consider PNP to be redone
          else:
            pgw.packet_tx(nasa_pnp_phase0_request_network_address())
            nasa_pnp_time=time.time()
            nasa_reset_state()
            nasa_pnp_ended=False
            nasa_pnp_check_requested=False
        # detect ASHP reboot and remote controller to execute PNP again
        if nasa_pnp_ended and not nasa_pnp_check_requested and nasa_pnp_time + NASA_PNP_CHECK_INTERVAL < time.time():
          # request reading of ZONE1 TEMP (expect a reponse with it, not the regular notification)
          pgw.packet_tx(nasa_read(0x4203))
          nasa_pnp_time=time.time()
          nasa_pnp_check_retries=0
          nasa_pnp_check_requested=True

    except BaseException as e:
      log.error(e, exc_info=True)
    time.sleep(1)

def mqtt_startup_thread():
  global mqtt_client
  def on_connect(client, userdata, flags, rc):
    global nasa_state
    if rc==0:
      mqtt_setup()
      nasa_reset_state()
      pass

  mqtt_client = mqtt.Client(clean_session=True)
  mqtt_client.on_connect=on_connect
  # initial connect may fail if mqtt server is not running
  # post power outage, it may occur the mqtt server is unreachable until
  # after the current script is executed
  while True:
    try:
      mqtt_client.connect(args.mqtt_host, args.mqtt_port)
      mqtt_client.loop_start()
      mqtt_setup()
      break
    except BaseException as e:
      log.error(e, exc_info=True)
    time.sleep(1) 

def mqtt_create_topic(nasa_msgnum, topic_config, device_class, name, topic_state, unit_name, type_handler, topic_set=None, desc_base={}, handler_parameter=None):
  config_content={}
  if desc_base is None:
    desc_base = {}
  for k in desc_base:
    config_content[k] = desc_base[k]
  config_content["name"]= name
  topic='notopic'
  if topic_set:
    topic=topic_set
    config_content["command_topic"] = topic_set
  if topic_state:
    topic=topic_state
    config_content["state_topic"] = topic_state
  if device_class:
    config_content["device_class"] = device_class
  if unit_name:
    config_content["unit_of_measurement"] = unit_name

  log.info(topic + " = " + json.dumps(config_content))
  mqtt_client.publish(topic_config, 
    payload=json.dumps(config_content), 
    retain=True)

  nasa_name = nasa_message_name(nasa_msgnum)
  if not nasa_name in mqtt_published_vars:
    if handler_parameter is None: 
      handler = type_handler(mqtt_client, topic, nasa_msgnum)
    else:
      handler = type_handler(mqtt_client, topic, nasa_msgnum, handler_parameter)
    mqtt_published_vars[nasa_name] = handler
  
  handler = mqtt_published_vars[nasa_name]
  if topic_set:
    mqtt_client.message_callback_add(topic_set, handler.action)
    mqtt_client.subscribe(topic_set)
  
  return handler

def mqtt_setup():
  mqtt_create_topic(0x202, 'homeassistant/sensor/samsung_ehs_error_code_1/config', None, 'Samsung EHS Error Code 1', 'homeassistant/sensor/samsung_ehs_error_code_1/state', None, MQTTHandler, None)

  mqtt_create_topic(0x4427, 'homeassistant/sensor/samsung_ehs_total_output_power/config', 'energy', 'Samsung EHS Total Output Power', 'homeassistant/sensor/samsung_ehs_total_output_power/state', 'W', MQTTHandler, None, {"state_class": "total_increasing"})
  mqtt_create_topic(0x8414, 'homeassistant/sensor/samsung_ehs_total_input_power/config', 'energy', 'Samsung EHS Total Input Power', 'homeassistant/sensor/samsung_ehs_total_input_power/state', 'W', MQTTHandler, None, {"state_class": "total_increasing"})
  
  mqtt_create_topic(0x4426, 'homeassistant/sensor/samsung_ehs_current_output_power/config', 'energy', 'Samsung EHS Output Power', 'homeassistant/sensor/samsung_ehs_current_output_power/state', 'W', MQTTHandler, None)
  mqtt_create_topic(0x8413, 'homeassistant/sensor/samsung_ehs_current_input_power/config', 'energy', 'Samsung EHS Input Power', 'homeassistant/sensor/samsung_ehs_current_input_power/state', 'W', COPMQTTHandler, None)
  # special value published by the COPMQTTHandler
  mqtt_client.publish('homeassistant/sensor/samsung_ehs_cop/config', 
    payload=json.dumps({"name": "Samsung EHS Operating COP", 
                        "state_topic": 'homeassistant/sensor/samsung_ehs_current_input_power/state_cop'}), 
    retain=True)
  # minimum flow set to 10% to avoid LWT raising exponentially
  mqtt_create_topic(0x40C4, 'homeassistant/number/samsung_ehs_inv_pump_pwm/config', 'power_factor', 'Samsung EHS Inverter Pump PWM', 'homeassistant/number/samsung_ehs_inv_pump_pwm/state', '%', FSVSetMQTTHandler, 'homeassistant/number/samsung_ehs_inv_pump_pwm/set', {"min": 10, "max": 100, "step": 1})

  mqtt_create_topic(0x4202, 'homeassistant/sensor/samsung_ehs_temp_water_target/config', 'temperature', 'Samsung EHS Water Target', 'homeassistant/sensor/samsung_ehs_temp_water_target/state', '°C', SetMQTTHandler, None, None, 10)

  mqtt_create_topic(0x4236, 'homeassistant/sensor/samsung_ehs_temp_water_in/config', 'temperature', 'Samsung EHS RWT Water In', 'homeassistant/sensor/samsung_ehs_temp_water_in/state', '°C', SetMQTTHandler, None, None, 10)
  mqtt_create_topic(0x4238, 'homeassistant/sensor/samsung_ehs_temp_water_out/config', 'temperature', 'Samsung EHS LWT Water Out', 'homeassistant/sensor/samsung_ehs_temp_water_out/state', '°C', SetMQTTHandler, None, None, 10)
  mqtt_create_topic(0x420C, 'homeassistant/sensor/samsung_ehs_temp_outer/config', 'temperature', 'Samsung EHS Temp Outer', 'homeassistant/sensor/samsung_ehs_temp_outer/state', '°C', SetMQTTHandler, None, None, 10)
  mqtt_create_topic(0x4205, 'homeassistant/sensor/samsung_ehs_temp_eva_in/config', 'temperature', 'Samsung EHS Temp EVA In', 'homeassistant/sensor/samsung_ehs_temp_eva_in/state', '°C', SetMQTTHandler, None, None, 10)
  mqtt_create_topic(0x428C, 'homeassistant/sensor/samsung_ehs_temp_mixing_valve_zone1/config', 'temperature', 'Samsung EHS Temp Mixing Valve Zone1', 'homeassistant/sensor/samsung_ehs_temp_mixing_valve_zone1/state', '°C', SetMQTTHandler, None, None, 10)
  mqtt_create_topic(0x42E9, 'homeassistant/sensor/samsung_ehs_water_flow/config', 'volume_flow_rate', 'Samsung EHS Water Flow', 'homeassistant/sensor/samsung_ehs_water_flow/state', 'L/min', SetMQTTHandler, None, None, 10)
  mqtt_create_topic(0x4028, 'homeassistant/binary_sensor/samsung_ehs_op/config', 'running', 'Samsung EHS Operating', 'homeassistant/binary_sensor/samsung_ehs_op/state', None, ONOFFSetMQTTHandler)
  mqtt_create_topic(0x402E, 'homeassistant/binary_sensor/samsung_ehs_defrosting_op/config', 'running', 'Samsung EHS Defrosting', 'homeassistant/binary_sensor/samsung_ehs_defrosting_op/state', None, ONOFFSetMQTTHandler)
  mqtt_create_topic(0x82FE, 'homeassistant/sensor/samsung_ehs_water_pressure/config', 'pressure', 'Samsung EHS Water Pressure', 'homeassistant/sensor/samsung_ehs_water_pressure/state', 'bar', SetMQTTHandler, None, None, 100)
  
  mqtt_create_topic(0x427F, 'homeassistant/sensor/samsung_ehs_temp_water_law_target/config', 'temperature', 'Samsung EHS Temp Water Law Target', 'homeassistant/sensor/samsung_ehs_temp_water_law_target/state', '°C', SetMQTTHandler, None, None, 10)

  # EHS mode
  optmap={"automatic (0)":0, "cooling (1)":1, "heating (4)":4}
  mqtt_create_topic(0x4001, 'homeassistant/select/samsung_ehs_mode/config', None, 'Samsung EHS Mode', 'homeassistant/select/samsung_ehs_mode/state', None, FSVStringIntMQTTHandler, 'homeassistant/select/samsung_ehs_mode/set', {"options": [*optmap]}, optmap)

  mqtt_create_topic(0x4000, 'homeassistant/switch/samsung_ehs_zone1/config', None, 'Samsung EHS Zone1', 'homeassistant/switch/samsung_ehs_zone1/state', None, Zone1SwitchMQTTHandler, 'homeassistant/switch/samsung_ehs_zone1/set')
  mqtt_create_topic(0x4201, 'homeassistant/number/samsung_ehs_temp_zone1_target/config', 'temperature', 'Samsung EHS Zone1 Target', 'homeassistant/number/samsung_ehs_temp_zone1_target/state', '°C', SetMQTTHandler, 'homeassistant/number/samsung_ehs_temp_zone1_target/set', {"min": 16, "max": 28, "step": 0.5}, 10)
  #mqtt_create_topic(0x423A, 'homeassistant/number/samsung_ehs_temp_zone1/config', 'temperature', 'Samsung EHS Zone1 Ambient', 'homeassistant/number/samsung_ehs_temp_zone1/state', '°C', Zone1IntDiv10MQTTHandler, 'homeassistant/number/samsung_ehs_temp_zone1/set')
  mqtt_create_topic(0x4203, 'homeassistant/number/samsung_ehs_temp_zone1/config', 'temperature', 'Samsung EHS Zone1 Ambient', 'homeassistant/number/samsung_ehs_temp_zone1/state', '°C', Zone1IntDiv10MQTTHandler, 'homeassistant/number/samsung_ehs_temp_zone1/set')
  mqtt_create_topic(0x42D8, 'homeassistant/sensor/samsung_ehs_temp_outlet_zone1/config', 'temperature', 'Samsung EHS Temp Outlet Zone1', 'homeassistant/sensor/samsung_ehs_temp_outlet_zone1/state', '°C', SetMQTTHandler, None, None, 10)
  
  mqtt_create_topic(0x411e, 'homeassistant/switch/samsung_ehs_zone2/config', None, 'Samsung EHS Zone2', 'homeassistant/switch/samsung_ehs_zone2/state', None, Zone2SwitchMQTTHandler, 'homeassistant/switch/samsung_ehs_zone2/set')
  mqtt_create_topic(0x42D6, 'homeassistant/number/samsung_ehs_temp_zone2_target/config', 'temperature', 'Samsung EHS Zone2 Target', 'homeassistant/number/samsung_ehs_temp_zone2_target/state', '°C', SetMQTTHandler, 'homeassistant/number/samsung_ehs_temp_zone2_target/set', {"min": 16, "max": 28, "step": 0.5}, 10)
  #mqtt_create_topic(0x42DA, 'homeassistant/number/samsung_ehs_temp_zone2/config', 'temperature', 'Samsung EHS Zone2 Ambient', 'homeassistant/number/samsung_ehs_temp_zone2/state', '°C', Zone2IntDiv10MQTTHandler, 'homeassistant/number/samsung_ehs_temp_zone2/set')
  mqtt_create_topic(0x42D4, 'homeassistant/number/samsung_ehs_temp_zone2/config', 'temperature', 'Samsung EHS Zone2 Ambient', 'homeassistant/number/samsung_ehs_temp_zone2/state', '°C', Zone2IntDiv10MQTTHandler, 'homeassistant/number/samsung_ehs_temp_zone2/set')
  mqtt_create_topic(0x42D9, 'homeassistant/sensor/samsung_ehs_temp_outlet_zone2/config', 'temperature', 'Samsung EHS Temp Outlet Zone2', 'homeassistant/sensor/samsung_ehs_temp_outlet_zone2/state', '°C', SetMQTTHandler, None, None, 10)

  mqtt_create_topic(0x4235, 'homeassistant/number/samsung_ehs_temp_dhw_target/config', 'temperature', 'Samsung EHS Temp DHW Target', 'homeassistant/number/samsung_ehs_temp_dhw_target/state', '°C', SetMQTTHandler, 'homeassistant/number/samsung_ehs_temp_dhw_target/set', {"min": 35, "max": 70, "step": 1}, 10)
  mqtt_create_topic(0x4065, 'homeassistant/switch/samsung_ehs_dhw/config', None, 'Samsung EHS DHW', 'homeassistant/switch/samsung_ehs_dhw/state', None, DHWONOFFMQTTHandler, 'homeassistant/switch/samsung_ehs_dhw/set')
  mqtt_create_topic(0x4237, 'homeassistant/sensor/samsung_ehs_temp_dhw/config', 'temperature', 'Samsung EHS Temp DHW Tank', 'homeassistant/sensor/samsung_ehs_temp_dhw/state', '°C', SetMQTTHandler, None, None, 10)

  # notify of script start
  topic_state = 'homeassistant/sensor/samsung_ehs_mqtt_bridge/date'
  mqtt_client.publish('homeassistant/sensor/samsung_ehs_mqtt_bridge/config', payload=json.dumps({'state_topic':topic_state,'name':'Samsung EHS Bridge restart date'}), retain=True)
  mqtt_client.publish('homeassistant/sensor/samsung_ehs_mqtt_bridge/date', payload=datetime.strftime(datetime.now(), "%Y%m%d%H%M%S"), retain=True)

  # Raw payload sending
  topic_payload_state = 'homeassistant/text/samsung_ehs_payload/state'
  topic_payload_set = 'homeassistant/text/samsung_ehs_payload/set'
  mqtt_client.message_callback_add(topic_payload_set, nasa_payload_mqtt_handler)
  mqtt_client.subscribe(topic_payload_set)
  mqtt_client.publish('homeassistant/text/samsung_ehs_payload/config', 
    payload=json.dumps({'command_topic':topic_payload_set,
                        'state_topic':topic_payload_state,
                        'name':'Samsung EHS Payload'}), 
    retain=True)

  # FSV unlock toggle to avoid unwanted finger modifications :)
  topic_state = 'homeassistant/switch/samsung_ehs_fsv_unlock/state'
  topic_set = 'homeassistant/switch/samsung_ehs_fsv_unlock/set'
  mqtt_client.message_callback_add(topic_set, nasa_fsv_unlock_mqtt_handler)
  mqtt_client.subscribe(topic_set)
  mqtt_client.publish('homeassistant/switch/samsung_ehs_fsv_unlock/config', 
    payload=json.dumps({'command_topic':topic_set,
                        'state_topic':topic_state,
                        'name':'Samsung EHS FSV Unlock'}), 
    retain=True)
  # relock by default
  global nasa_fsv_unlocked
  nasa_fsv_unlocked=False
  mqtt_client.publish(topic_state, 'OFF')

  # FSV values
  optmap={"only ambient (1)":1, "thermo off -> pump off (2)":2, "thermo off -> pump on (3)":3, "thermo off -> 70% pump on (4)":4}
  mqtt_create_topic(0x4127, 'homeassistant/select/samsung_ehs_2093_tempctrl/config', None, 'Samsung EHS FSV2093 Temp Control', 'homeassistant/select/samsung_ehs_2093_tempctrl/state', None, FSVStringIntMQTTHandler, 'homeassistant/select/samsung_ehs_2093_tempctrl/set', {"options": [*optmap]}, optmap)
  optmap={"no inverter pump (0)":0, "inverter pump use 100% (1)":1, "inverter pump use 70% (2)":2}
  mqtt_create_topic(0x40C2, 'homeassistant/select/samsung_ehs_4051_inv_pump_ctrl/config', None, 'Samsung EHS FSV4051 Inverter Pump Control', 'homeassistant/select/samsung_ehs_4051_inv_pump_ctrl/state', None, FSVStringIntMQTTHandler, 'homeassistant/select/samsung_ehs_4051_inv_pump_ctrl/set', {"options": [*optmap]}, optmap)
  mqtt_create_topic(0x428A, 'homeassistant/number/samsung_ehs_4052_dt_target/config', 'temperature', 'Samsung EHS FSV4052 dT Target', 'homeassistant/number/samsung_ehs_4052_dt_target/state', '°C', FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_4052_dt_target/set', {"min": 2, "max": 8, "step": 1})
  mqtt_create_topic(0x40C3, 'homeassistant/number/samsung_ehs_4053_inv_pump_factor/config', None, 'Samsung EHS FSV4053 Inverter Pump Factor', 'homeassistant/number/samsung_ehs_4053_inv_pump_factor/state', None, FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_4053_inv_pump_factor/set', {"min": 1, "max": 3, "step": 1})
  # DHW
  optmap={"no DHW":0, "DHW thermo on temp (1)":1, "DHW thermo off temp (2)": 2}
  mqtt_create_topic(0x4097, 'homeassistant/select/samsung_ehs_3011_dhw_ctrl/config', None, 'Samsung EHS FSV3011 DHW Control', 'homeassistant/select/samsung_ehs_3011_dhw_ctrl/state', None, FSVStringIntMQTTHandler, 'homeassistant/select/samsung_ehs_3011_dhw_ctrl/set', {"options": [*optmap]}, optmap)
  mqtt_create_topic(0x4260, 'homeassistant/number/samsung_ehs_3021_dhw_max_temp/config', None, 'Samsung EHS FSV3021 DHW Max Temp', 'homeassistant/number/samsung_ehs_3021_dhw_max_temp/state', None, FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_3021_dhw_max_temp/set', {"min": 0, "max": 70, "step": 1}, 10)
  mqtt_create_topic(0x4261, 'homeassistant/number/samsung_ehs_3022_dhw_stop_temp/config', None, 'Samsung EHS FSV3022 DHW Stop Temp', 'homeassistant/number/samsung_ehs_3022_dhw_stop_temp/state', None, FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_3022_dhw_stop_temp/set', {"min": 0, "max": 10, "step": 1}, 10)
  mqtt_create_topic(0x4262, 'homeassistant/number/samsung_ehs_3023_dhw_start_temp/config', None, 'Samsung EHS FSV3023 DHW Start Temp', 'homeassistant/number/samsung_ehs_3023_dhw_start_temp/state', None, FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_3023_dhw_start_temp/set', {"min": 0, "max": 30, "step": 1}, 10)
  mqtt_create_topic(0x4263, 'homeassistant/number/samsung_ehs_3024_dhw_sh_min_sh_time/config', None, 'Samsung EHS FSV3024 DHW+SH Min Heating Duration', 'homeassistant/number/samsung_ehs_3024_dhw_sh_min_time/state', None, FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_3024_dhw_sh_min_time/set', {"min": 1, "max": 20, "step": 1})
  mqtt_create_topic(0x4264, 'homeassistant/number/samsung_ehs_3025_dhw_sh_max_dhw_time/config', None, 'Samsung EHS FSV3025 DHW+SH Max DHW Duration', 'homeassistant/number/samsung_ehs_3025_dhw_sh_max_dhw_time/state', None, FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_3025_dhw_sh_max_dhw_time/set', {"min": 5, "max": 95, "step": 5})
  mqtt_create_topic(0x4265, 'homeassistant/number/samsung_ehs_3026_dhw_sh_max_sh_time/config', None, 'Samsung EHS FSV3026 DHW+SH Max Heating Duration', 'homeassistant/number/samsung_ehs_3026_dhw_sh_max_sh_time/state', None, FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_3026_dhw_sh_max_sh_time/set', {"min": 30, "max": 600, "step": 30})
  optmap={"no booster (0)":0, "booster used (1)":1}
  mqtt_create_topic(0x4098, 'homeassistant/select/samsung_ehs_3031_dhw_booster_ctrl/config', None, 'Samsung EHS FSV3031 DHW Booster Control', 'homeassistant/select/samsung_ehs_3031_dhw_booster_ctrl/state', None, FSVStringIntMQTTHandler, 'homeassistant/select/samsung_ehs_3031_dhw_booster_ctrl/set', {"options": [*optmap]}, optmap)
  mqtt_create_topic(0x4099, 'homeassistant/number/samsung_ehs_3041_dhw_disinfect/config', None, 'Samsung EHS FSV3041 DHW Disinfection', 'homeassistant/number/samsung_ehs_3041_dhw_disinfect/state', None, FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_3041_dhw_disinfect/set', {"min": 0, "max": 1, "step": 1})
  mqtt_create_topic(0x409B, 'homeassistant/number/samsung_ehs_3051_dhw_forced_timer_off/config', None, 'Samsung EHS FSV3051 DHW Forced Timer OFF', 'homeassistant/number/samsung_ehs_3051_dhw_forced_timer_off/state', None, FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_3051_dhw_forced_timer_off/set', {"min": 0, "max": 1, "step": 1}) 
  mqtt_create_topic(0x426C, 'homeassistant/number/samsung_ehs_3052_dhw_forced_timer_off_duration/config', None, 'Samsung EHS FSV3052 DHW Forced Timer Duration', 'homeassistant/number/samsung_ehs_3052_dhw_forced_timer_off_duration/state', None, FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_3052_dhw_forced_timer_off_duration/set', {"min": 0, "max": 30, "step": 1})
  optmap={"floor heating(1)":1, "fan coil unit or radiator (2)":2}
  mqtt_create_topic(0x4093, 'homeassistant/select/samsung_ehs_2041_wl/config', None, 'Samsung EHS FSV2041 Water Law', 'homeassistant/select/samsung_ehs_2041_wl/state', None, FSVStringIntMQTTHandler, 'homeassistant/select/samsung_ehs_2041_wl/set', {"options": [*optmap]}, optmap)

  #heating
  mqtt_create_topic(0x4254, 'homeassistant/number/samsung_ehs_2011_wlmax/config', 'temperature', 'Samsung EHS FSV2011 Heating Water Law Max', 'homeassistant/number/samsung_ehs_2011_wlmax/state', '°C', FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_2011_wlmax/set', {"min": -20, "max": 5, "step": 1}, 10)
  mqtt_create_topic(0x4255, 'homeassistant/number/samsung_ehs_2012_wlmin/config', 'temperature', 'Samsung EHS FSV2012 Heating Water Law Min', 'homeassistant/number/samsung_ehs_2012_wlmin/state', '°C', FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_2012_wlmin/set', {"min": 10, "max": 20, "step": 1}, 10)
  mqtt_create_topic(0x4256, 'homeassistant/number/samsung_ehs_2021_wl1max/config', 'temperature', 'Samsung EHS FSV2021 Heating Water Out WL1 Temp Max', 'homeassistant/number/samsung_ehs_2021_wl1max/state', '°C', FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_2021_wl1max/set', {"min": 17, "max": 75, "step": 1}, 10)
  mqtt_create_topic(0x4257, 'homeassistant/number/samsung_ehs_2022_wl1min/config', 'temperature', 'Samsung EHS FSV2022 Heating Water Out WL1 Temp Min', 'homeassistant/number/samsung_ehs_2022_wl1min/state', '°C', FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_2022_wl1min/set', {"min": 17, "max": 75, "step": 1}, 10)
  mqtt_create_topic(0x4258, 'homeassistant/number/samsung_ehs_2031_wl2max/config', 'temperature', 'Samsung EHS FSV2031 Heating Water Out WL2 Temp Max', 'homeassistant/number/samsung_ehs_2031_wl2max/state', '°C', FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_2031_wl2max/set', {"min": 17, "max": 75, "step": 1}, 10)
  mqtt_create_topic(0x4259, 'homeassistant/number/samsung_ehs_2032_wl2min/config', 'temperature', 'Samsung EHS FSV2032 Heating Water Out WL2 Temp Min', 'homeassistant/number/samsung_ehs_2032_wl2min/state', '°C', FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_2032_wl2min/set', {"min": 17, "max": 75, "step": 1}, 10)
  #cooling
  mqtt_create_topic(0x425A, 'homeassistant/number/samsung_ehs_2051_wlmax/config', 'temperature', 'Samsung EHS FSV2051 Cooling Water Law Max', 'homeassistant/number/samsung_ehs_2051_wlmax/state', '°C', FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_2051_wlmax/set', {"min": 25, "max": 35, "step": 1}, 10)
  mqtt_create_topic(0x425B, 'homeassistant/number/samsung_ehs_2052_wlmin/config', 'temperature', 'Samsung EHS FSV2052 Cooling Water Law Min', 'homeassistant/number/samsung_ehs_2052_wlmin/state', '°C', FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_2052_wlmin/set', {"min": 35, "max": 45, "step": 1}, 10)
  mqtt_create_topic(0x425C, 'homeassistant/number/samsung_ehs_2061_wl1max/config', 'temperature', 'Samsung EHS FSV2061 Cooling Water Out WL1 Temp Max', 'homeassistant/number/samsung_ehs_2061_wl1max/state', '°C', FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_2061_wl1max/set', {"min": 5, "max": 25, "step": 1}, 10)
  mqtt_create_topic(0x425D, 'homeassistant/number/samsung_ehs_2062_wl1min/config', 'temperature', 'Samsung EHS FSV2062 Cooling Water Out WL1 Temp Min', 'homeassistant/number/samsung_ehs_2062_wl1min/state', '°C', FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_2062_wl1min/set', {"min": 5, "max": 25, "step": 1}, 10)
  mqtt_create_topic(0x425E, 'homeassistant/number/samsung_ehs_2071_wl2max/config', 'temperature', 'Samsung EHS FSV2071 Cooling Water Out WL2 Temp Max', 'homeassistant/number/samsung_ehs_2071_wl2max/state', '°C', FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_2071_wl2max/set', {"min": 5, "max": 25, "step": 1}, 10)
  mqtt_create_topic(0x425F, 'homeassistant/number/samsung_ehs_2072_wl2min/config', 'temperature', 'Samsung EHS FSV2072 Cooling Water Out WL2 Temp Min', 'homeassistant/number/samsung_ehs_2072_wl2min/state', '°C', FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_2072_wl2min/set', {"min": 5, "max": 25, "step": 1}, 10)

  #mixing valve settings
  optmap={"no mixing valve (0)":0, "valve with offset (1)":1, "valve 2 water laws (2)":2}
  mqtt_create_topic(0x40C0, 'homeassistant/select/samsung_ehs_4041_V3V_enabled/config', None, 'Samsung EHS FSV4041 Mixing Valve Enabled', 'homeassistant/select/samsung_ehs_4041_V3V_enabled/state', None, FSVStringIntMQTTHandler, 'homeassistant/select/samsung_ehs_4041_V3V_enabled/set', {"options": [*optmap]}, optmap)
  mqtt_create_topic(0x4286, 'homeassistant/number/samsung_ehs_4042_V3V_dt/config', None, 'Samsung EHS FSV4042 Mixing Valve dT Heating', 'homeassistant/number/samsung_ehs_4042_V3V_dt/state', None, FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_4042_V3V_dt/set', {"min": 5, "max": 15, "step": 1}, 10)
  mqtt_create_topic(0x4287, 'homeassistant/number/samsung_ehs_4043_V3V_dt/config', None, 'Samsung EHS FSV4043 Mixing Valve dT Cooling', 'homeassistant/number/samsung_ehs_4043_V3V_dt/state', None, FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_4043_V3V_dt/set', {"min": 5, "max": 15, "step": 1}, 10)
  mqtt_create_topic(0x40C1, 'homeassistant/number/samsung_ehs_4044_V3V_factor/config', None, 'Samsung EHS FSV4044 Mixing Valve Factor', 'homeassistant/number/samsung_ehs_4044_V3V_factor/state', None, FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_4044_V3V_factor/set', {"min": 1, "max": 5, "step": 1})
  mqtt_create_topic(0x4288, 'homeassistant/number/samsung_ehs_4045_V3V_interval/config', None, 'Samsung EHS FSV4045 Mixing Valve Interval (minute)', 'homeassistant/number/samsung_ehs_4045_V3V_interval/state', None, FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_4045_V3V_interval/set', {"min": 1, "max": 30, "step": 1})
  mqtt_create_topic(0x4289, 'homeassistant/number/samsung_ehs_4046_V3V_runtime/config', None, 'Samsung EHS FSV4046 Mixing Valve Running Time (x10 seconds)', 'homeassistant/number/samsung_ehs_4046_V3V_runtime/state', None, FSVWriteMQTTHandler, 'homeassistant/number/samsung_ehs_4046_V3V_runtime/set', {"min": 6, "max": 24, "step": 3})
  optmap={"disabled (0)":0, "enabled (1)":1}
  mqtt_create_topic(0x411a, 'homeassistant/select/samsung_ehs_zone_control/config', None, 'Samsung EHS FSV4061 Zone Control', 'homeassistant/select/samsung_ehs_zone_control/state', None, FSVStringIntMQTTHandler, 'homeassistant/select/samsung_ehs_zone_control/set', {"options": [*optmap]}, optmap)
  # Use values from the DHW valve FSV 3071, 0 is room, 1 is tank
  optmap={"0":0, "1":1}
  mqtt_create_topic(0x408b, 'homeassistant/sensor/samsung_ehs_dhw_3way_valve_dir/config', None, 'Samsung EHS DHW Valve Direction Tank', 'homeassistant/sensor/samsung_ehs_dhw_3way_valve_dir/state', None, FSVStringIntMQTTHandler, None, {"options": [*optmap]}, optmap)

  # unknown values to be traced to reverse them
  mqtt_create_topic(0x4090, 'homeassistant/sensor/samsung_ehs_4090/config', None, 'Samsung EHS 0x4090 Air efficiency', 'homeassistant/sensor/samsung_ehs_4090/state', None, FSVWriteMQTTHandler, None)
  mqtt_create_topic(0x40b2, 'homeassistant/sensor/samsung_ehs_40b2/config', None, 'Samsung EHS 0x40b2', 'homeassistant/sensor/samsung_ehs_40b2/state', None, FSVWriteMQTTHandler, None)

  mqtt_create_topic(0x4046, 'homeassistant/switch/samsung_ehs_silence_mode/config', None, 'Samsung EHS Silence Mode', 'homeassistant/switch/samsung_ehs_silence_mode/state', None, FSVONOFFMQTTHandler, 'homeassistant/switch/samsung_ehs_silence_mode/set')
  mqtt_create_topic(0x4129, 'homeassistant/switch/samsung_ehs_silence_param/config', None, 'Samsung EHS Silence Parameter', 'homeassistant/switch/samsung_ehs_silence_param/state', None, FSVONOFFMQTTHandler, 'homeassistant/switch/samsung_ehs_silence_param/set')

threading.Thread(name="publisher", target=publisher_thread).start()
if not args.dump_only:
  threading.Thread(name="mqtt_startup", target=mqtt_startup_thread).start()

log.info("-----------------------------------------------------------------")
log.info("Startup")


"""
TODO:
- detect loss of communication from the ASHP
- test DHW temp after powercycle, to ensure the request is sufficient to persist the value in the Controller




"""
