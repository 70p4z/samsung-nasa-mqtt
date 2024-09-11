#!/bin/bash
#sleep 15
cd $(dirname $(readlink -f $0))
screen -dmS samsung_nasa bash -c 'bash run.sh'
