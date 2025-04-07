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
parser.add_argument('--nasa-timeout', default="60", type=auto_int, help="Timeout before considering communication fault")
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
  except:
    traceback.print_exc()
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
    mqtt_client.publish('homeassistant/text/samsung_ehs_payload/state', mqttpayload)
    # will prepend start, compute and append crc and stop
    pgw.packet_tx(binpayload)
  except:
    traceback.print_exc()

def nasa_fsv_writable():
  global nasa_fsv_unlocked
  return nasa_fsv_unlocked

def nasa_fsv_unlock_mqtt_handler(client, userdata, msg):
  global nasa_fsv_unlocked
  mqttpayload = msg.payload.decode('utf-8')
  if mqttpayload == "ON":
    mqtt_client.publish('homeassistant/switch/samsung_ehs_fsv_unlock/state', 'ON')
    nasa_fsv_unlocked=True
  else:
    mqtt_client.publish('homeassistant/switch/samsung_ehs_fsv_unlock/state', 'OFF')
    nasa_fsv_unlocked=False

class MQTTHandler():
  def __init__(self, mqtt_client, topic, nasa_msgnum):
    self.topic = topic
    self.nasa_msgnum = nasa_msgnum
    self.mqtt_client = mqtt_client

  def publish(self, valueInt):
    self.mqtt_client.publish(self.topic, valueInt)

  def action(self, client, userdata, msg):
    pass

  def initread(self):
    pass

class FSVWriteMQTTHandler(MQTTHandler):
  def publish(self, valueInt):
    self.mqtt_client.publish(self.topic, valueInt)

class FSVWrite1MQTTHandler(FSVWriteMQTTHandler):
  def action(self, client, userdata, msg):
    if not nasa_fsv_writable():
      return
    intval = int(float(msg.payload.decode('utf-8')))
    if nasa_update(self.nasa_msgnum, intval):
      global pgw
      pgw.packet_tx(nasa_write_u8(self.nasa_msgnum, intval))
  def initread(self):
    global pgw
    pgw.packet_tx(nasa_read_u8(self.nasa_msgnum))

class FSVWrite2MQTTHandler(FSVWriteMQTTHandler):
  def action(self, client, userdata, msg):
    if not nasa_fsv_writable():
      return
    intval = int(float(msg.payload.decode('utf-8')))
    if nasa_update(self.nasa_msgnum, intval):
      global pgw
      pgw.packet_tx(nasa_write_u16(self.nasa_msgnum, intval))
  def initread(self):
    global pgw
    pgw.packet_tx(nasa_read_u16(self.nasa_msgnum))

class FSVWrite2Div10MQTTHandler(FSVWriteMQTTHandler):
  def publish(self, valueInt):
    self.mqtt_client.publish(self.topic, valueInt/10.0)
  def action(self, client, userdata, msg):
    if not nasa_fsv_writable():
      return
    intval = int(float(msg.payload.decode('utf-8'))*10)
    if nasa_update(self.nasa_msgnum, intval):
      global pgw
      pgw.packet_tx(nasa_write_u16(self.nasa_msgnum, intval))
  def initread(self):
    global pgw
    pgw.packet_tx(nasa_read_u16(self.nasa_msgnum))

class Uint8MQTTHandler(MQTTHandler):
  def publish(self, valueInt):
    self.mqtt_client.publish(self.topic, valueInt)

  def action(self, client, userdata, msg):
    intval = int(float(msg.payload.decode('utf-8')))
    if nasa_update(self.nasa_msgnum, intval):
      global pgw
      pgw.packet_tx(nasa_set_u8(self.nasa_msgnum, intval))

class FSVLockUint8MQTTHandler(Uint8MQTTHandler):
  def action(self, client, userdata, msg):
    if not nasa_fsv_writable():
      return
    super().action(client, userdata, msg)

class IntDiv10MQTTHandler(MQTTHandler):
  def publish(self, valueInt):
    self.mqtt_client.publish(self.topic, valueInt/10.0)

  def action(self, client, userdata, msg):
    intval = int(float(msg.payload.decode('utf-8'))*10)
    if nasa_update(self.nasa_msgnum, intval):
      global pgw
      pgw.packet_tx(nasa_set_u16(self.nasa_msgnum, intval))
      
class IntDiv100MQTTHandler(MQTTHandler):
  def publish(self, valueInt):
    self.mqtt_client.publish(self.topic, valueInt/100.0)

class ONOFFMQTTHandler(MQTTHandler):
  def publish(self, valueInt):
    valueStr = "ON"
    if valueInt==0:
      valueStr="OFF"
    self.mqtt_client.publish(self.topic, valueStr)

class DHWONOFFMQTTHandler(ONOFFMQTTHandler):
  def action(self, client, userdata, msg):
    mqttpayload = msg.payload.decode('utf-8')
    intval=0
    if mqttpayload == "ON":
      intval=1
    if nasa_update(self.nasa_msgnum, intval):
      global pgw
      pgw.packet_tx(nasa_dhw_power(intval == 1))

class COPMQTTHandler(MQTTHandler):
  def publish(self, valueInt):
    self.mqtt_client.publish(self.topic, valueInt)
    # compute COP and publish the value as well
    # round at 2 digits
    try:
      if valueInt == 0:
        valueInt = 14
      self.mqtt_client.publish(self.topic + "_cop", int(nasa_state[nasa_message_name(0x4426)]*100 / valueInt)/100)
    except:
      pass

class Zone1IntDiv10MQTTHandler(IntDiv10MQTTHandler):
  def action(self, client, userdata, msg):
    global nasa_state
    mqttpayload = msg.payload.decode('utf-8')
    self.mqtt_client.publish(self.topic, mqttpayload)
    new_temp = int(float(mqttpayload)*10)
    if nasa_update(0x423A, new_temp):
      global pgw
      pgw.packet_tx(nasa_set_zone1_temperature(float(mqttpayload)))

def ehs_get_mode(default_mode="HOT"):
  global nasa_state
  try:
    # variable holding the current mode
    nasa_name = nasa_message_name(0x4001)
    if not nasa_name in nasa_state:
      return default_mode
    modeint = int(nasa_state[nasa_name])
    if modeint == 0:
      return "AUTO"
    elif modeint == 1:
      return "COLD"
  except:
    pass
  return default_mode

class EHSModeMQTTHandler(MQTTHandler):
  def publish(self, valueInt):
    if valueInt == 0:
      self.mqtt_client.publish(self.topic, "Auto")
    elif valueInt == 1:
      self.mqtt_client.publish(self.topic, "Cold")
    elif valueInt == 4:
      self.mqtt_client.publish(self.topic, "Hot")

  def action(self, client, userdata, msg):
    global pgw
    payload = msg.payload.decode('utf-8')
    if payload == "Auto":
      if nasa_update(self.nasa_msgnum, 0):
        pgw.packet_tx(nasa_set_u8(self.nasa_msgnum, 0))
    elif payload == "Cold":
      if nasa_update(self.nasa_msgnum, 1):
        pgw.packet_tx(nasa_set_u8(self.nasa_msgnum, 1))
    else: # Hot is default
      if nasa_update(self.nasa_msgnum, 4):
        pgw.packet_tx(nasa_set_u8(self.nasa_msgnum, 4))

class Zone1SwitchMQTTHandler(ONOFFMQTTHandler):
  def action(self, client, userdata, msg):
    global nasa_state
    mqttpayload = msg.payload.decode('utf-8')
    global pgw
    enabled = mqttpayload == "ON"
    pgw.packet_tx(nasa_zone_power(enabled,1))

class Zone2IntDiv10MQTTHandler(IntDiv10MQTTHandler):
  def action(self, client, userdata, msg):
    global nasa_state
    mqttpayload = msg.payload.decode('utf-8')
    self.mqtt_client.publish(self.topic, mqttpayload)
    new_temp = int(float(mqttpayload)*10)
    if nasa_update(0x42DA, new_temp):
      global pgw
      pgw.packet_tx(nasa_set_zone2_temperature(float(mqttpayload)))
      
class Zone2SwitchMQTTHandler(ONOFFMQTTHandler):
  def action(self, client, userdata, msg):
    global nasa_state
    mqttpayload = msg.payload.decode('utf-8')
    global pgw
    enabled = mqttpayload == "ON"
    pgw.packet_tx(nasa_zone_power(enabled,2))

#handler(source, dest, isInfo, protocolVersion, retryCounter, packetType, payloadType, packetNumber, dataSets)
def rx_nasa_handler(*nargs, **kwargs):
  global mqtt_client
  global last_nasa_rx
  global args
  global pgw
  global nasa_pnp_check_requested
  global nasa_pnp_ended
  last_nasa_rx = time.time()
  packetType = kwargs["packetType"]
  payloadType = kwargs["payloadType"]
  packetNumber = kwargs["packetNumber"]
  dataSets = kwargs["dataSets"]
  source = kwargs["source"]
  dest = kwargs["dest"]
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

      # detect PNP check's response
      if args.nasa_pnp:
        if nasa_pnp_ended and nasa_pnp_check_requested and payloadType == "response" and tools.bin2hex(source) == "200000" and ds[0] == 0x4229:
          nasa_pnp_check_requested=False

      # hold the value indexed by its name, for easier update of mqtt stuff
      # (set the int raw value)
      nasa_state[ds[1]] = ds[4][0]

      if ds[1] in mqtt_published_vars:
        # use the topic name and payload formatter from the mqtt publish array
        mqtt_p_v = mqtt_published_vars[ds[1]]
        mqtt_p_v.publish(ds[4][0])

      mqtt_client.publish('homeassistant/sensor/samsung_ehs/nasa_'+hex(ds[0]), payload=ds[2], retain=True)
    except:
      traceback.print_exc()

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
  # wait until IOs are setup
  time.sleep(10)
  nasa_last_publish = 0

  if not args.nasa_pnp:
    nasa_set_attributed_address(args.nasa_addr)

  while True:
    try:
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
            if not nasa_message_name(handler.nasa_msgnum) in nasa_state and isinstance(handler, FSVWriteMQTTHandler):
              handler.initread()
              time.sleep(0.1)
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
            pgw.packet_tx(nasa_read_u16(0x4229))
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
          # request reading of MODEL INFORMATION (expect a reponse with it, not the regular notification)
          pgw.packet_tx(nasa_read_u16(0x4229))
          nasa_pnp_time=time.time()
          nasa_pnp_check_retries=0
          nasa_pnp_check_requested=True

    except:
      traceback.print_exc()
    # handle communication timeout
    if last_nasa_rx + args.nasa_timeout < time.time():
      log.info("Communication lost!")
      os.kill(os.getpid(), signal.SIGTERM)

    time.sleep(5)

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
    except:
      traceback.print_exc()
    time.sleep(1) 

def mqtt_create_topic(nasa_msgnum, topic_config, device_class, name, topic_state, unit_name, type_handler, topic_set, desc_base={}):
  config_content={}
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
    handler = type_handler(mqtt_client, topic, nasa_msgnum)
    mqtt_published_vars[nasa_name] = handler
  
  handler = mqtt_published_vars[nasa_name]
  if topic_set:
    mqtt_client.message_callback_add(topic_set, handler.action)
    mqtt_client.subscribe(topic_set)
  
  return handler

def mqtt_setup():
  mqtt_create_topic(0x202, 'homeassistant/sensor/samsung_ehs_error_code_1/config', None, 'Samsung EHS Error Code 1', 'homeassistant/sensor/samsung_ehs_error_code_1/state', None, MQTTHandler, None)

  mqtt_create_topic(0x4427, 'homeassistant/sensor/samsung_ehs_total_output_power/config', 'energy', 'Samsung EHS Total Output Power', 'homeassistant/sensor/samsung_ehs_total_output_power/state', 'Wh', MQTTHandler, None)
  mqtt_create_topic(0x8414, 'homeassistant/sensor/samsung_ehs_total_input_power/config', 'energy', 'Samsung EHS Total Input Power', 'homeassistant/sensor/samsung_ehs_total_input_power/state', 'Wh', MQTTHandler, None)
  
  mqtt_create_topic(0x4426, 'homeassistant/sensor/samsung_ehs_current_output_power/config', 'energy', 'Samsung EHS Output Power', 'homeassistant/sensor/samsung_ehs_current_output_power/state', 'Wh', MQTTHandler, None)
  mqtt_create_topic(0x8413, 'homeassistant/sensor/samsung_ehs_current_input_power/config', 'energy', 'Samsung EHS Input Power', 'homeassistant/sensor/samsung_ehs_current_input_power/state', 'Wh', COPMQTTHandler, None)
  # special value published by the COPMQTTHandler
  mqtt_client.publish('homeassistant/sensor/samsung_ehs_cop/config', 
    payload=json.dumps({"name": "Samsung EHS Operating COP", 
                        "state_topic": 'homeassistant/sensor/samsung_ehs_current_input_power/state_cop'}), 
    retain=True)
  # minimum flow set to 10% to avoid LWT raising exponentially
  mqtt_create_topic(0x40C4, 'homeassistant/number/samsung_ehs_inv_pump_pwm/config', 'power_factor', 'Samsung EHS Inverter Pump PWM', 'homeassistant/number/samsung_ehs_inv_pump_pwm/state', '%', FSVLockUint8MQTTHandler, 'homeassistant/number/samsung_ehs_inv_pump_pwm/set', {"min": 10, "max": 100, "step": 1})

  mqtt_create_topic(0x4202, 'homeassistant/sensor/samsung_ehs_temp_water_target/config', 'temperature', 'Samsung EHS Water Target', 'homeassistant/sensor/samsung_ehs_temp_water_target/state', '°C', IntDiv10MQTTHandler, None)

  mqtt_create_topic(0x4236, 'homeassistant/sensor/samsung_ehs_temp_water_in/config', 'temperature', 'Samsung EHS RWT Water In', 'homeassistant/sensor/samsung_ehs_temp_water_in/state', '°C', IntDiv10MQTTHandler, None)
  mqtt_create_topic(0x4238, 'homeassistant/sensor/samsung_ehs_temp_water_out/config', 'temperature', 'Samsung EHS LWT Water Out', 'homeassistant/sensor/samsung_ehs_temp_water_out/state', '°C', IntDiv10MQTTHandler, None)
  mqtt_create_topic(0x420C, 'homeassistant/sensor/samsung_ehs_temp_outer/config', 'temperature', 'Samsung EHS Temp Outer', 'homeassistant/sensor/samsung_ehs_temp_outer/state', '°C', IntDiv10MQTTHandler, None)
  mqtt_create_topic(0x4205, 'homeassistant/sensor/samsung_ehs_temp_eva_in/config', 'temperature', 'Samsung EHS Temp EVA In', 'homeassistant/sensor/samsung_ehs_temp_eva_in/state', '°C', IntDiv10MQTTHandler, None)
  mqtt_create_topic(0x428C, 'homeassistant/sensor/samsung_ehs_temp_mixing_valve_zone1/config', 'temperature', 'Samsung EHS Temp Mixing Valve Zone1', 'homeassistant/sensor/samsung_ehs_temp_mixing_valve_zone1/state', '°C', IntDiv10MQTTHandler, None)
  mqtt_create_topic(0x42E9, 'homeassistant/sensor/samsung_ehs_water_flow/config', 'volume_flow_rate', 'Samsung EHS Water Flow', 'homeassistant/sensor/samsung_ehs_water_flow/state', 'L/min', IntDiv10MQTTHandler, None)
  mqtt_create_topic(0x4028, 'homeassistant/binary_sensor/samsung_ehs_op/config', 'running', 'Samsung EHS Operating', 'homeassistant/binary_sensor/samsung_ehs_op/state', None, ONOFFMQTTHandler, None)
  mqtt_create_topic(0x402E, 'homeassistant/binary_sensor/samsung_ehs_defrosting_op/config', 'running', 'Samsung EHS Defrosting', 'homeassistant/binary_sensor/samsung_ehs_defrosting_op/state', None, ONOFFMQTTHandler, None)
  mqtt_create_topic(0x82FE, 'homeassistant/sensor/samsung_ehs_water_pressure/config', 'pressure', 'Samsung EHS Water Pressure', 'homeassistant/sensor/samsung_ehs_water_pressure/state', 'bar', IntDiv100MQTTHandler, None)
  
  mqtt_create_topic(0x427F, 'homeassistant/sensor/samsung_ehs_temp_water_law_target/config', 'temperature', 'Samsung EHS Temp Water Law Target', 'homeassistant/sensor/samsung_ehs_temp_water_law_target/state', '°C', IntDiv10MQTTHandler, None)

  # EHS mode
  mqtt_create_topic(0x4001, 'homeassistant/select/samsung_ehs_mode/config', None, 'Samsung EHS Mode', 'homeassistant/select/samsung_ehs_mode/state', None, EHSModeMQTTHandler, 'homeassistant/select/samsung_ehs_mode/set', {"options": ["Auto", "Cold", "Hot"]})

  mqtt_create_topic(0x4000, 'homeassistant/switch/samsung_ehs_zone1/config', None, 'Samsung EHS Zone1', 'homeassistant/switch/samsung_ehs_zone1/state', None, Zone1SwitchMQTTHandler, 'homeassistant/switch/samsung_ehs_zone1/set')
  mqtt_create_topic(0x4201, 'homeassistant/number/samsung_ehs_temp_zone1_target/config', 'temperature', 'Samsung EHS Temp Zone1 Target', 'homeassistant/number/samsung_ehs_temp_zone1_target/state', '°C', IntDiv10MQTTHandler, 'homeassistant/number/samsung_ehs_temp_zone1_target/set', {"min": 16, "max": 28, "step": 0.5})
  mqtt_create_topic(0x423A, 'homeassistant/number/samsung_ehs_temp_zone1/config', 'temperature', 'Samsung EHS Temp Zone1', 'homeassistant/number/samsung_ehs_temp_zone1/state', '°C', Zone1IntDiv10MQTTHandler, 'homeassistant/number/samsung_ehs_temp_zone1/set')
  mqtt_create_topic(0x42D8, 'homeassistant/sensor/samsung_ehs_temp_outlet_zone1/config', 'temperature', 'Samsung EHS Temp Outlet Zone1', 'homeassistant/sensor/samsung_ehs_temp_outlet_zone1/state', '°C', IntDiv10MQTTHandler, None)
  
  mqtt_create_topic(0x411e, 'homeassistant/switch/samsung_ehs_zone2/config', None, 'Samsung EHS Zone2', 'homeassistant/switch/samsung_ehs_zone2/state', None, Zone2SwitchMQTTHandler, 'homeassistant/switch/samsung_ehs_zone2/set')
  mqtt_create_topic(0x42D6, 'homeassistant/number/samsung_ehs_temp_zone2_target/config', 'temperature', 'Samsung EHS Temp Zone2 Target', 'homeassistant/number/samsung_ehs_temp_zone2_target/state', '°C', IntDiv10MQTTHandler, 'homeassistant/number/samsung_ehs_temp_zone2_target/set', {"min": 16, "max": 28, "step": 0.5})
  mqtt_create_topic(0x42DA, 'homeassistant/number/samsung_ehs_temp_zone2/config', 'temperature', 'Samsung EHS Temp Zone2', 'homeassistant/number/samsung_ehs_temp_zone2/state', '°C', Zone2IntDiv10MQTTHandler, 'homeassistant/number/samsung_ehs_temp_zone2/set')
  mqtt_create_topic(0x42D9, 'homeassistant/sensor/samsung_ehs_temp_outlet_zone2/config', 'temperature', 'Samsung EHS Temp Outlet Zone2', 'homeassistant/sensor/samsung_ehs_temp_outlet_zone2/state', '°C', IntDiv10MQTTHandler, None)

  mqtt_create_topic(0x4235, 'homeassistant/number/samsung_ehs_temp_dhw_target/config', 'temperature', 'Samsung EHS Temp DHW Target', 'homeassistant/number/samsung_ehs_temp_dhw_target/state', '°C', IntDiv10MQTTHandler, 'homeassistant/number/samsung_ehs_temp_dhw_target/set', {"min": 35, "max": 70, "step": 1})
  mqtt_create_topic(0x4065, 'homeassistant/switch/samsung_ehs_dhw/config', None, 'Samsung EHS DHW', 'homeassistant/switch/samsung_ehs_dhw/state', None, DHWONOFFMQTTHandler, 'homeassistant/switch/samsung_ehs_dhw/set')
  mqtt_create_topic(0x4237, 'homeassistant/sensor/samsung_ehs_temp_dhw/config', 'temperature', 'Samsung EHS Temp DHW Tank', 'homeassistant/sensor/samsung_ehs_temp_dhw/state', '°C', IntDiv10MQTTHandler, None)

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
  mqtt_create_topic(0x4127, 'homeassistant/number/samsung_ehs_2093_tempctrl/config', None, 'Samsung EHS FSV2093 Temp Control', 'homeassistant/number/samsung_ehs_2093_tempctrl/state', None, FSVWrite1MQTTHandler, 'homeassistant/number/samsung_ehs_2093_tempctrl/set', {"min": 1, "max": 4, "step": 1})
  mqtt_create_topic(0x40C2, 'homeassistant/number/samsung_ehs_4051_inv_pump_ctrl/config', None, 'Samsung EHS FSV4051 Inverter Pump Control', 'homeassistant/number/samsung_ehs_4051_inv_pump_ctrl/state', None, FSVWrite1MQTTHandler, 'homeassistant/number/samsung_ehs_4051_inv_pump_ctrl/set', {"min": 0, "max": 2, "step": 1})
  mqtt_create_topic(0x428A, 'homeassistant/number/samsung_ehs_4052_dt_target/config', 'temperature', 'Samsung EHS FSV4052 dT Target', 'homeassistant/number/samsung_ehs_4052_dt_target/state', '°C', FSVWrite2MQTTHandler, 'homeassistant/number/samsung_ehs_4052_dt_target/set', {"min": 2, "max": 8, "step": 1})
  mqtt_create_topic(0x40C3, 'homeassistant/number/samsung_ehs_4053_inv_pump_factor/config', None, 'Samsung EHS FSV4053 Inverter Pump Factor', 'homeassistant/number/samsung_ehs_4053_inv_pump_factor/state', None, FSVWrite1MQTTHandler, 'homeassistant/number/samsung_ehs_4053_inv_pump_factor/set', {"min": 1, "max": 3, "step": 1})
  # DHW
  mqtt_create_topic(0x4097, 'homeassistant/number/samsung_ehs_3011_dhw_ctrl/config', None, 'Samsung EHS FSV3011 DHW Control', 'homeassistant/number/samsung_ehs_3011_dhw_ctrl/state', None, FSVWrite1MQTTHandler, 'homeassistant/number/samsung_ehs_3011_dhw_ctrl/set', {"min": 0, "max": 2, "step": 1})
  mqtt_create_topic(0x4260, 'homeassistant/number/samsung_ehs_3021_dhw_max_temp/config', None, 'Samsung EHS FSV3021 DHW Max Temp', 'homeassistant/number/samsung_ehs_3021_dhw_max_temp/state', None, FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_3021_dhw_max_temp/set', {"min": 0, "max": 70, "step": 1})
  mqtt_create_topic(0x4261, 'homeassistant/number/samsung_ehs_3022_dhw_stop_temp/config', None, 'Samsung EHS FSV3022 DHW Stop Temp', 'homeassistant/number/samsung_ehs_3022_dhw_stop_temp/state', None, FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_3022_dhw_stop_temp/set', {"min": 0, "max": 10, "step": 1})
  mqtt_create_topic(0x4262, 'homeassistant/number/samsung_ehs_3023_dhw_start_temp/config', None, 'Samsung EHS FSV3023 DHW Start Temp', 'homeassistant/number/samsung_ehs_3023_dhw_start_temp/state', None, FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_3023_dhw_start_temp/set', {"min": 0, "max": 30, "step": 1})
  mqtt_create_topic(0x4263, 'homeassistant/number/samsung_ehs_3024_dhw_sh_min_sh_time/config', None, 'Samsung EHS FSV3024 DHW+SH Min Heating Duration', 'homeassistant/number/samsung_ehs_3024_dhw_sh_min_time/state', None, FSVWrite2MQTTHandler, 'homeassistant/number/samsung_ehs_3024_dhw_sh_min_time/set', {"min": 1, "max": 20, "step": 1})
  mqtt_create_topic(0x4264, 'homeassistant/number/samsung_ehs_3025_dhw_sh_max_dhw_time/config', None, 'Samsung EHS FSV3025 DHW+SH Max DHW Duration', 'homeassistant/number/samsung_ehs_3025_dhw_sh_max_dhw_time/state', None, FSVWrite2MQTTHandler, 'homeassistant/number/samsung_ehs_3025_dhw_sh_max_dhw_time/set', {"min": 5, "max": 95, "step": 5})
  mqtt_create_topic(0x4265, 'homeassistant/number/samsung_ehs_3026_dhw_sh_max_sh_time/config', None, 'Samsung EHS FSV3026 DHW+SH Max Heating Duration', 'homeassistant/number/samsung_ehs_3026_dhw_sh_max_sh_time/state', None, FSVWrite2MQTTHandler, 'homeassistant/number/samsung_ehs_3026_dhw_sh_max_sh_time/set', {"min": 30, "max": 600, "step": 30})
  mqtt_create_topic(0x4098, 'homeassistant/number/samsung_ehs_3031_dhw_booster_ctrl/config', None, 'Samsung EHS FSV3031 DHW Booster Control', 'homeassistant/number/samsung_ehs_3031_dhw_booster_ctrl/state', None, FSVWrite1MQTTHandler, 'homeassistant/number/samsung_ehs_3031_dhw_booster_ctrl/set', {"min": 0, "max": 2, "step": 1})
  mqtt_create_topic(0x4099, 'homeassistant/number/samsung_ehs_3041_dhw_disinfect/config', None, 'Samsung EHS FSV3041 DHW Disinfection', 'homeassistant/number/samsung_ehs_3041_dhw_disinfect/state', None, FSVWrite1MQTTHandler, 'homeassistant/number/samsung_ehs_3041_dhw_disinfect/set', {"min": 0, "max": 1, "step": 1})
  mqtt_create_topic(0x409B, 'homeassistant/number/samsung_ehs_3051_dhw_forced_timer_off/config', None, 'Samsung EHS FSV3051 DHW Forced Timer OFF', 'homeassistant/number/samsung_ehs_3051_dhw_forced_timer_off/state', None, FSVWrite1MQTTHandler, 'homeassistant/number/samsung_ehs_3051_dhw_forced_timer_off/set', {"min": 0, "max": 1, "step": 1}) 
  mqtt_create_topic(0x426C, 'homeassistant/number/samsung_ehs_3052_dhw_forced_timer_off_duration/config', None, 'Samsung EHS FSV3052 DHW Forced Timer Duration', 'homeassistant/number/samsung_ehs_3052_dhw_forced_timer_off_duration/state', None, FSVWrite2MQTTHandler, 'homeassistant/number/samsung_ehs_3052_dhw_forced_timer_off_duration/set', {"min": 0, "max": 30, "step": 1})
  mqtt_create_topic(0x4093, 'homeassistant/number/samsung_ehs_2041_wl/config', None, 'Samsung EHS FSV2041 Water Law', 'homeassistant/number/samsung_ehs_2041_wl/state', None, FSVWrite1MQTTHandler, 'homeassistant/number/samsung_ehs_2041_wl/set', {"min": 1, "max": 2, "step": 1})
  #heating
  mqtt_create_topic(0x4254, 'homeassistant/number/samsung_ehs_2011_wlmax/config', 'temperature', 'Samsung EHS FSV2011 Heating Water Law Max', 'homeassistant/number/samsung_ehs_2011_wlmax/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2011_wlmax/set', {"min": -20, "max": 5, "step": 1})
  mqtt_create_topic(0x4255, 'homeassistant/number/samsung_ehs_2012_wlmin/config', 'temperature', 'Samsung EHS FSV2012 Heating Water Law Min', 'homeassistant/number/samsung_ehs_2012_wlmin/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2012_wlmin/set', {"min": 10, "max": 20, "step": 1})
  mqtt_create_topic(0x4256, 'homeassistant/number/samsung_ehs_2021_wl1max/config', 'temperature', 'Samsung EHS FSV2021 Heating Water Out WL1 Temp Max', 'homeassistant/number/samsung_ehs_2021_wl1max/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2021_wl1max/set', {"min": 17, "max": 75, "step": 1})
  mqtt_create_topic(0x4257, 'homeassistant/number/samsung_ehs_2022_wl1min/config', 'temperature', 'Samsung EHS FSV2022 Heating Water Out WL1 Temp Min', 'homeassistant/number/samsung_ehs_2022_wl1min/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2022_wl1min/set', {"min": 17, "max": 75, "step": 1})
  mqtt_create_topic(0x4258, 'homeassistant/number/samsung_ehs_2031_wl2max/config', 'temperature', 'Samsung EHS FSV2031 Heating Water Out WL2 Temp Max', 'homeassistant/number/samsung_ehs_2031_wl2max/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2031_wl2max/set', {"min": 17, "max": 75, "step": 1})
  mqtt_create_topic(0x4259, 'homeassistant/number/samsung_ehs_2032_wl2min/config', 'temperature', 'Samsung EHS FSV2032 Heating Water Out WL2 Temp Min', 'homeassistant/number/samsung_ehs_2032_wl2min/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2032_wl2min/set', {"min": 17, "max": 75, "step": 1})
  #cooling
  mqtt_create_topic(0x425A, 'homeassistant/number/samsung_ehs_2051_wlmax/config', 'temperature', 'Samsung EHS FSV2051 Cooling Water Law Max', 'homeassistant/number/samsung_ehs_2051_wlmax/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2051_wlmax/set', {"min": 25, "max": 35, "step": 1})
  mqtt_create_topic(0x425B, 'homeassistant/number/samsung_ehs_2052_wlmin/config', 'temperature', 'Samsung EHS FSV2052 Cooling Water Law Min', 'homeassistant/number/samsung_ehs_2052_wlmin/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2052_wlmin/set', {"min": 35, "max": 45, "step": 1})
  mqtt_create_topic(0x425C, 'homeassistant/number/samsung_ehs_2061_wl1max/config', 'temperature', 'Samsung EHS FSV2061 Cooling Water Out WL1 Temp Max', 'homeassistant/number/samsung_ehs_2061_wl1max/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2061_wl1max/set', {"min": 5, "max": 25, "step": 1})
  mqtt_create_topic(0x425D, 'homeassistant/number/samsung_ehs_2062_wl1min/config', 'temperature', 'Samsung EHS FSV2062 Cooling Water Out WL1 Temp Min', 'homeassistant/number/samsung_ehs_2062_wl1min/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2062_wl1min/set', {"min": 5, "max": 25, "step": 1})
  mqtt_create_topic(0x425E, 'homeassistant/number/samsung_ehs_2071_wl2max/config', 'temperature', 'Samsung EHS FSV2071 Cooling Water Out WL2 Temp Max', 'homeassistant/number/samsung_ehs_2071_wl2max/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2071_wl2max/set', {"min": 5, "max": 25, "step": 1})
  mqtt_create_topic(0x425F, 'homeassistant/number/samsung_ehs_2072_wl2min/config', 'temperature', 'Samsung EHS FSV2072 Cooling Water Out WL2 Temp Min', 'homeassistant/number/samsung_ehs_2072_wl2min/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2072_wl2min/set', {"min": 5, "max": 25, "step": 1})

  #mixing valve settings
  mqtt_create_topic(0x40C0, 'homeassistant/number/samsung_ehs_4041_V3V_enabled/config', None, 'Samsung EHS FSV4041 Mixing Valve Enabled', 'homeassistant/number/samsung_ehs_4041_V3V_enabled/state', None, FSVWrite1MQTTHandler, 'homeassistant/number/samsung_ehs_4041_V3V_enabled/set', {"min": 0, "max": 2, "step": 1})
  mqtt_create_topic(0x4286, 'homeassistant/number/samsung_ehs_4042_V3V_dt/config', None, 'Samsung EHS FSV4042 Mixing Valve dT Heating', 'homeassistant/number/samsung_ehs_4042_V3V_dt/state', None, FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_4042_V3V_dt/set', {"min": 5, "max": 15, "step": 1})
  mqtt_create_topic(0x4287, 'homeassistant/number/samsung_ehs_4043_V3V_dt/config', None, 'Samsung EHS FSV4043 Mixing Valve dT Cooling', 'homeassistant/number/samsung_ehs_4043_V3V_dt/state', None, FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_4043_V3V_dt/set', {"min": 5, "max": 15, "step": 1})
  mqtt_create_topic(0x40C1, 'homeassistant/number/samsung_ehs_4044_V3V_factor/config', None, 'Samsung EHS FSV4044 Mixing Valve Factor', 'homeassistant/number/samsung_ehs_4044_V3V_factor/state', None, FSVWrite1MQTTHandler, 'homeassistant/number/samsung_ehs_4044_V3V_factor/set', {"min": 1, "max": 5, "step": 1})
  mqtt_create_topic(0x4288, 'homeassistant/number/samsung_ehs_4045_V3V_interval/config', None, 'Samsung EHS FSV4045 Mixing Valve Interval (minute)', 'homeassistant/number/samsung_ehs_4045_V3V_interval/state', None, FSVWrite2MQTTHandler, 'homeassistant/number/samsung_ehs_4045_V3V_interval/set', {"min": 1, "max": 30, "step": 1})
  mqtt_create_topic(0x4289, 'homeassistant/number/samsung_ehs_4046_V3V_runtime/config', None, 'Samsung EHS FSV4046 Mixing Valve Running Time (x10 seconds)', 'homeassistant/number/samsung_ehs_4046_V3V_runtime/state', None, FSVWrite2MQTTHandler, 'homeassistant/number/samsung_ehs_4046_V3V_runtime/set', {"min": 6, "max": 24, "step": 3})
  mqtt_create_topic(0x411a, 'homeassistant/number/samsung_ehs_zone_control/config', None, 'Samsung EHS FSV4061 Zone Control', 'homeassistant/number/samsung_ehs_zone_control/state', None, FSVWrite1MQTTHandler, 'homeassistant/number/samsung_ehs_zone_control/set', {"min": 0, "max": 2, "step": 1})  
  # Use values from the DHW valve FSV 3071, 0 is room, 1 is tank
  mqtt_create_topic(0x408b, 'homeassistant/sensor/samsung_ehs_dhw_3way_valve_dir/config', None, 'Samsung EHS DHW Valve Direction Tank', 'homeassistant/sensor/samsung_ehs_dhw_3way_valve_dir/state', None, FSVWrite1MQTTHandler, None)

  # unknown values to be traced to reverse them
  mqtt_create_topic(0x4090, 'homeassistant/sensor/samsung_ehs_4090/config', None, 'Samsung EHS 0x4090', 'homeassistant/sensor/samsung_ehs_4090/state', None, Uint8MQTTHandler, None)
  mqtt_create_topic(0x40b2, 'homeassistant/sensor/samsung_ehs_40b2/config', None, 'Samsung EHS 0x40b2', 'homeassistant/sensor/samsung_ehs_40b2/state', None, Uint8MQTTHandler, None)

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
