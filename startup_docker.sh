#!/bin/bash
#sleep 15
cd $(dirname $(readlink -f $0))
screen -dmS samsung_nasa bash -c 'while [ true ] ; do bash run.sh --mqtt-host host.docker.internal ; sleep 1 ; done'
