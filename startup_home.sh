#!/bin/bash
#sleep 15
cd $(dirname $(readlink -f $0))
screen -dmS samsung_nasa bash -c 'while [ true ] ; do bash run.sh --mqtt-host 192.168.0.4 ; sleep 1 ; done'
