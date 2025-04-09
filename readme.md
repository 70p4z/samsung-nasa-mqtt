## About the project
At first I was just willing to switch on/off the DHW relying on Home Assistant as I hacked previously with my gas boiler.
After a few thinking, I wanted to ditch the wire for remote controller, to allow for free placement of the temperature sensors (i.e. samsung wireless nasa).
In the end, I ended up rewriting a NASA-MQTT bridge in python, allowing all the previous, and many more.

## Hardware parts
The project has been tested in 2 different configurations:

### Through F3/F4 pair
I'm connecting using the F3/F4 pair, and a wireless raspberry pi zero w.

The tricky part was to find how to communicate with the F3/F4 bus, using a THVD8000 proved useful and efficient.

I'm designing a PCB to allow for USB<->F3/F4 to allow for more people to play with this.

### Through F1/F2 pair
It allows extracting many information from the AHSP. Also it's not yet suited to fully control it through that link.

## How to
Just execute the samsung_mqtt_home_assistant.py script after tweaking its values (extended configuration means to come)

## Demo
Here is a screenshot of the available items for my Samsung Mono R290 5kW (AE050CXYBEK/EU)
![alt Home Assistant screenshot](https://github.com/70p4z/samsung-nasa-mqtt/blob/main/home_assistant_list.png?raw=true)

## Future evolutions

Many things are under construction:
  - A PCB for remote controller to go wireless (still using a plug adapter, but wirelessing the F3/F4 bus)
  - Home Assistant scripts to allow for aggregating multiple temperature sensor and set the zone 1/2 current temp according to user rules (min of, max of ...)
  - more testing and control through F1/F2, there is a MITM I'd like to conduct to better heat the DHW, this is to reduce the water flow temperature to what is known to work but not too hot to avoid killing the CoP.

## Credits

- Super project to get started on protocol stuff [https://wiki.myehs.eu/wiki/NASA_Protocol]
- Another bridging device [https://github.com/lanwin/esphome_samsung_ac]
- For some ideas [https://github.com/Foxhill67/nasa2mqtt]
