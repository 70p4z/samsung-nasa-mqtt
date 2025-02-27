#!/bin/bash
cd $(dirname $(readlink -f $0))
#sleep 15 

#serial=/dev/serial/by-id/usb-STMicroelectronics_STM32_STLink_066BFF554857707067031522-if02
serial=/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A600JLJ7-if00-port0
serial=/dev/serial/by-id/usb-0483_5740-if00
serial=/dev/serial/by-id/`ls /dev/serial/by-id/ | head -n1`
socat $serial,raw,echo=0,nonblock,min=0,b9600,parenb tcp-listen:7001,reuseaddr,fork &

python3 samsung_mqtt_home_assistant.py &

wait -n
pkill -P $$
