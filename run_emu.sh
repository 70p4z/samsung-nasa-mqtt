#!/bin/bash
cd $(dirname $(readlink -f $0))
#sleep 15 

#serial=/dev/serial/by-id/usb-FTDI_FT232R_USB_UART_A5069RR4-if00-port0
baud=9600
serial=/dev/serial/by-id/`ls /dev/serial/by-id/ |  head -n1`
socat $serial,raw,echo=0,nonblock,min=0,b$baud,parenb,ioctl-void=0x540c tcp-listen:7002,reuseaddr,fork &

python3 samsung_nasa_indoor_emu.py

#wait -n
pkill -P $$
