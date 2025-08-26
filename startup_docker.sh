#!/bin/bash
cd $(dirname $(readlink -f $0))
while [ true ]
do 
	bash run.sh --mqtt-host host.docker.internal 
	sleep 1 
done
