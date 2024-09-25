import packetgateway
import os
import tools
import logging
import time
import threading 
import argparse
import traceback
import paho.mqtt.client as mqtt
import json
import sys
import signal

from nasa_messages import *

LOGLEVEL = os.environ.get('LOGLEVEL', 'INFO').upper()
LOGFORMAT = '%(asctime)s %(levelname)s %(threadName)s %(message)s'
logging.basicConfig(format=LOGFORMAT)
log = logging.getLogger("samsung_nasa")
log.setLevel(LOGLEVEL)

def auto_int(x):
  return int(x, 0)

parser = argparse.ArgumentParser()
parser.add_argument('--mqtt-host', default="192.168.0.4", help="host to connect to the MQTT broker")
parser.add_argument('--mqtt-port', default="1883", type=auto_int, help="port of the MQTT broker")
parser.add_argument('--serial-host', default="127.0.0.1",help="host to connect the serial interface endpoint (i.e. socat /dev/ttyUSB0,parenb,raw,echo=0,b9600,nonblock,min=0 tcp-listen:7001,reuseaddr,fork )")
parser.add_argument('--serial-port', default="7001", type=auto_int, help="port to connect the serial interface endpoint")
parser.add_argument('--nasa-interval', default="30", type=auto_int, help="Interval in seconds to republish MQTT values set from the MQTT side (useful for temperature mainly)")
parser.add_argument('--nasa-timeout', default="60", type=auto_int, help="Timeout before considering communication fault")
args = parser.parse_args()

# NASA state
nasa_state = {}
mqtt_client = None
mqtt_published_vars = {}
pgw = None
last_nasa_rx = 0

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
    intval = int(float(msg.payload.decode('utf-8')))
    if nasa_update(self.nasa_msgnum, intval):
      global pgw
      pgw.packet_tx(nasa_write_u8(self.nasa_msgnum, intval))
  def initread(self):
    global pgw
    pgw.packet_tx(nasa_read_u8(self.nasa_msgnum))

class FSVWrite2MQTTHandler(FSVWriteMQTTHandler):
  def action(self, client, userdata, msg):
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
    intval = int(float(msg.payload.decode('utf-8'))*10)
    if nasa_update(self.nasa_msgnum, intval):
      global pgw
      pgw.packet_tx(nasa_write_u16(self.nasa_msgnum, intval))
  def initread(self):
    global pgw
    pgw.packet_tx(nasa_read_u16(self.nasa_msgnum))

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
    self.mqtt_client.publish(self.topic + "_cop", int(nasa_state[nasa_message_name(0x4426)]*100 / valueInt)/100)

class Zone1IntDiv10MQTTHandler(IntDiv10MQTTHandler):
  def action(self, client, userdata, msg):
    global nasa_state
    mqttpayload = msg.payload.decode('utf-8')
    self.mqtt_client.publish(self.topic, mqttpayload)
    new_temp = int(float(mqttpayload)*10)
    if nasa_update(0x423A, new_temp):
      global pgw
      pgw.packet_tx(nasa_set_zone1_temperature(float(mqttpayload)))

class Zone1HOTSwitchMQTTHandler(ONOFFMQTTHandler):
  def action(self, client, userdata, msg):
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
      
class Zone2HOTSwitchMQTTHandler(ONOFFMQTTHandler):
  def action(self, client, userdata, msg):
    mqttpayload = msg.payload.decode('utf-8')
    global pgw
    enabled = mqttpayload == "ON"
    pgw.packet_tx(nasa_zone_power(enabled,2))

#handler(source, dest, isInfo, protocolVersion, retryCounter, packetType, payloadType, packetNumber, dataSets)
def rx_nasa_handler(*args, **kwargs):
  global mqtt_client
  global last_nasa_rx
  last_nasa_rx = time.time()
  packetType = kwargs["packetType"]
  payloadType = kwargs["payloadType"]
  dataSets = kwargs["dataSets"]
  source = kwargs["source"]
  # ignore non normal packets
  if packetType != "normal":
    return
  # ignore read requests
  if payloadType != "notification" and payloadType != "write" and payloadType != "response":
    return

  for ds in dataSets:
    try:
      # we can tag the master's address
      if ( ds[1] == "NASA_IM_MASTER_NOTIFY" and ds[4][0] == 1) or (ds[1] == "NASA_IM_MASTER" and ds[4][0] == 1):
        nasa_state["master_address"] = source
        break
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
pgw = packetgateway.PacketGateway(args.serial_host, args.serial_port, rx_event=rx_event_nasa)
parser = packetgateway.NasaPacketParser()
pgw.start()
#ensure gateway is available and publish mqtt is possible when receving values
time.sleep(2)

# once in a while, publish zone2 current temp
def publisher_thread():
  global pgw
  global last_nasa_rx
  while True:
    time.sleep(args.nasa_interval)
    try:
      pgw.packet_tx(nasa_notify_error(0))
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

    except:
      traceback.print_exc()
    # handle communication timeout
    if last_nasa_rx + args.nasa_timeout < time.time():
      log.info("Communication lost!")
      os.kill(os.getpid(), signal.SIGTERM)

def mqtt_startup_thread():
  global mqtt_client
  def on_connect(client, userdata, flags, rc):
    global nasa_state
    if rc==0:
      mqtt_setup()
      nasa_state = {}
      pass

  mqtt_client = mqtt.Client('samsung_ehs',clean_session=True)
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

  if isinstance(handler, FSVWriteMQTTHandler):
    handler.initread()
  
  return handler

def mqtt_setup():
  mqtt_create_topic(0x4427, 'homeassistant/sensor/samsung_ehs_total_output_power/config', 'energy', 'Samsung EHS Total Output Power', 'homeassistant/sensor/samsung_ehs_total_output_power/state', 'Wh', MQTTHandler, None)
  mqtt_create_topic(0x8414, 'homeassistant/sensor/samsung_ehs_total_input_power/config', 'energy', 'Samsung EHS Total Input Power', 'homeassistant/sensor/samsung_ehs_total_input_power/state', 'Wh', MQTTHandler, None)
  
  mqtt_create_topic(0x4426, 'homeassistant/sensor/samsung_ehs_current_output_power/config', 'energy', 'Samsung EHS Output Power', 'homeassistant/sensor/samsung_ehs_current_output_power/state', 'Wh', MQTTHandler, None)
  mqtt_create_topic(0x8413, 'homeassistant/sensor/samsung_ehs_current_input_power/config', 'energy', 'Samsung EHS Input Power', 'homeassistant/sensor/samsung_ehs_current_input_power/state', 'Wh', COPMQTTHandler, None)
  # special value published by the COPMQTTHandler
  mqtt_client.publish('homeassistant/sensor/samsung_ehs_cop/config', 
    payload=json.dumps({"name": "Samsung EHS Operating COP", 
                        "state_topic": 'homeassistant/sensor/samsung_ehs_current_input_power/state_cop'}), 
    retain=True)

  mqtt_create_topic(0x4236, 'homeassistant/sensor/samsung_ehs_temp_water_in/config', 'temperature', 'Samsung EHS RWT Water In', 'homeassistant/sensor/samsung_ehs_temp_water_in/state', '°C', IntDiv10MQTTHandler, None)
  mqtt_create_topic(0x4238, 'homeassistant/sensor/samsung_ehs_temp_water_out/config', 'temperature', 'Samsung EHS LWT Water Out', 'homeassistant/sensor/samsung_ehs_temp_water_out/state', '°C', IntDiv10MQTTHandler, None)
  mqtt_create_topic(0x420C, 'homeassistant/sensor/samsung_ehs_temp_outer/config', 'temperature', 'Samsung EHS Temp Outer', 'homeassistant/sensor/samsung_ehs_temp_outer/state', '°C', IntDiv10MQTTHandler, None)
  mqtt_create_topic(0x4205, 'homeassistant/sensor/samsung_ehs_temp_eva_in/config', 'temperature', 'Samsung EHS Temp EVA In', 'homeassistant/sensor/samsung_ehs_temp_eva_in/state', '°C', IntDiv10MQTTHandler, None)
  mqtt_create_topic(0x428C, 'homeassistant/sensor/samsung_ehs_temp_mixing_valve_zone1/config', 'temperature', 'Samsung EHS Temp Mixing Valve Zone1', 'homeassistant/sensor/samsung_ehs_temp_mixing_valve_zone1/state', '°C', IntDiv10MQTTHandler, None)
  mqtt_create_topic(0x42E9, 'homeassistant/sensor/samsung_ehs_water_flow/config', 'volume_flow_rate', 'Samsung EHS Water Flow', 'homeassistant/sensor/samsung_ehs_water_flow/state', 'L/min', IntDiv10MQTTHandler, None)
  mqtt_create_topic(0x4028, 'homeassistant/binary_sensor/samsung_ehs_op/config', 'running', 'Samsung EHS Operating', 'homeassistant/binary_sensor/samsung_ehs_op/state', None, ONOFFMQTTHandler, None)
  mqtt_create_topic(0x402E, 'homeassistant/binary_sensor/samsung_ehs_defrosting_op/config', 'running', 'Samsung EHS Defrosting', 'homeassistant/binary_sensor/samsung_ehs_defrosting_op/state', None, ONOFFMQTTHandler, None)
  mqtt_create_topic(0x82FE, 'homeassistant/sensor/samsung_ehs_water_pressure/config', 'pressure', 'Samsung EHS Water Pressure', 'homeassistant/sensor/samsung_ehs_water_pressure/state', 'bar', IntDiv100MQTTHandler, None)
  
  mqtt_create_topic(0x4000, 'homeassistant/switch/samsung_ehs_zone1/config', None, 'Samsung EHS Zone1', 'homeassistant/switch/samsung_ehs_zone1/state', None, Zone1HOTSwitchMQTTHandler, 'homeassistant/switch/samsung_ehs_zone1/set')
  mqtt_create_topic(0x4201, 'homeassistant/number/samsung_ehs_temp_zone1_target/config', 'temperature', 'Samsung EHS Temp Zone1 Target', 'homeassistant/number/samsung_ehs_temp_zone1_target/state', '°C', IntDiv10MQTTHandler, 'homeassistant/number/samsung_ehs_temp_zone1_target/set', {"min": 16, "max": 28, "step": 0.5})
  mqtt_create_topic(0x423A, 'homeassistant/number/samsung_ehs_temp_zone1/config', 'temperature', 'Samsung EHS Temp Zone1', 'homeassistant/number/samsung_ehs_temp_zone1/state', '°C', Zone1IntDiv10MQTTHandler, 'homeassistant/number/samsung_ehs_temp_zone1/set')
  mqtt_create_topic(0x42D8, 'homeassistant/sensor/samsung_ehs_temp_outlet_zone1/config', 'temperature', 'Samsung EHS Temp Outlet Zone1', 'homeassistant/sensor/samsung_ehs_temp_outlet_zone1/state', '°C', IntDiv10MQTTHandler, None)
  
  mqtt_create_topic(0x411e, 'homeassistant/switch/samsung_ehs_zone2/config', None, 'Samsung EHS Zone2', 'homeassistant/switch/samsung_ehs_zone2/state', None, Zone2HOTSwitchMQTTHandler, 'homeassistant/switch/samsung_ehs_zone2/set')
  mqtt_create_topic(0x42D6, 'homeassistant/number/samsung_ehs_temp_zone2_target/config', 'temperature', 'Samsung EHS Temp Zone2 Target', 'homeassistant/number/samsung_ehs_temp_zone2_target/state', '°C', IntDiv10MQTTHandler, 'homeassistant/number/samsung_ehs_temp_zone2_target/set', {"min": 16, "max": 28, "step": 0.5})
  mqtt_create_topic(0x42DA, 'homeassistant/number/samsung_ehs_temp_zone2/config', 'temperature', 'Samsung EHS Temp Zone2', 'homeassistant/number/samsung_ehs_temp_zone2/state', '°C', Zone2IntDiv10MQTTHandler, 'homeassistant/number/samsung_ehs_temp_zone2/set')
  mqtt_create_topic(0x42D9, 'homeassistant/sensor/samsung_ehs_temp_outlet_zone2/config', 'temperature', 'Samsung EHS Temp Outlet Zone2', 'homeassistant/sensor/samsung_ehs_temp_outlet_zone2/state', '°C', IntDiv10MQTTHandler, None)

  mqtt_create_topic(0x4235, 'homeassistant/number/samsung_ehs_temp_dhw_target/config', 'temperature', 'Samsung EHS Temp DHW Target', 'homeassistant/number/samsung_ehs_temp_dhw_target/state', '°C', IntDiv10MQTTHandler, 'homeassistant/number/samsung_ehs_temp_dhw_target/set', {"min": 35, "max": 70, "step": 1})
  mqtt_create_topic(0x4065, 'homeassistant/switch/samsung_ehs_dhw/config', None, 'Samsung EHS DHW', 'homeassistant/switch/samsung_ehs_dhw/state', None, DHWONOFFMQTTHandler, 'homeassistant/switch/samsung_ehs_dhw/set')
  mqtt_create_topic(0x4237, 'homeassistant/sensor/samsung_ehs_temp_dhw/config', 'temperature', 'Samsung EHS Temp DHW Tank', 'homeassistant/sensor/samsung_ehs_temp_dhw/state', '°C', IntDiv10MQTTHandler, None)

  # FSV values
  mqtt_create_topic(0x428A, 'homeassistant/number/samsung_ehs_4052_dt_target/config', 'temperature', 'Samsung EHS FSV4052 dT Target', 'homeassistant/number/samsung_ehs_4052_dt_target/state', '°C', FSVWrite2MQTTHandler, 'homeassistant/number/samsung_ehs_4052_dt_target/set', {"min": 2, "max": 8, "step": 1})
  mqtt_create_topic(0x4093, 'homeassistant/number/samsung_ehs_2041_wl/config', None, 'Samsung EHS FSV2041 Water Law', 'homeassistant/number/samsung_ehs_2041_wl/state', None, FSVWrite1MQTTHandler, 'homeassistant/number/samsung_ehs_2041_wl/set', {"min": 1, "max": 2, "step": 1})
  mqtt_create_topic(0x4127, 'homeassistant/number/samsung_ehs_2093_tempctrl/config', None, 'Samsung EHS FSV2093 Temp Control', 'homeassistant/number/samsung_ehs_2093_tempctrl/state', None, FSVWrite1MQTTHandler, 'homeassistant/number/samsung_ehs_2093_tempctrl/set', {"min": 1, "max": 4, "step": 1})
  mqtt_create_topic(0x4254, 'homeassistant/number/samsung_ehs_2011_wlmax/config', 'temperature', 'Samsung EHS FSV2011 Water Law Max', 'homeassistant/number/samsung_ehs_2011_wlmax/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2011_wlmax/set', {"min": -20, "max": 5, "step": 1})
  mqtt_create_topic(0x4255, 'homeassistant/number/samsung_ehs_2012_wlmin/config', 'temperature', 'Samsung EHS FSV2012 Water Law Min', 'homeassistant/number/samsung_ehs_2012_wlmin/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2012_wlmin/set', {"min": 10, "max": 20, "step": 1})
  mqtt_create_topic(0x4256, 'homeassistant/number/samsung_ehs_2021_wl1max/config', 'temperature', 'Samsung EHS FSV2021 Water Out WL1 Temp Max', 'homeassistant/number/samsung_ehs_2021_wl1max/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2021_wl1max/set', {"min": 17, "max": 75, "step": 1})
  mqtt_create_topic(0x4257, 'homeassistant/number/samsung_ehs_2022_wl1min/config', 'temperature', 'Samsung EHS FSV2022 Water Out WL1 Temp Min', 'homeassistant/number/samsung_ehs_2022_wl1min/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2022_wl1min/set', {"min": 17, "max": 75, "step": 1})
  mqtt_create_topic(0x4258, 'homeassistant/number/samsung_ehs_2031_wl2max/config', 'temperature', 'Samsung EHS FSV2031 Water Out WL2 Temp Max', 'homeassistant/number/samsung_ehs_2031_wl2max/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2031_wl2max/set', {"min": 17, "max": 75, "step": 1})
  mqtt_create_topic(0x4259, 'homeassistant/number/samsung_ehs_2032_wl2min/config', 'temperature', 'Samsung EHS FSV2032 Water Out WL2 Temp Min', 'homeassistant/number/samsung_ehs_2032_wl2min/state', '°C', FSVWrite2Div10MQTTHandler, 'homeassistant/number/samsung_ehs_2032_wl2min/set', {"min": 17, "max": 75, "step": 1})

threading.Thread(name="publisher", target=publisher_thread).start()
threading.Thread(name="mqtt_startup", target=mqtt_startup_thread).start()

log.info("-----------------------------------------------------------------")
log.info("Startup")

"""
TODO:
- detect loss of communication from the ASHP





"""
