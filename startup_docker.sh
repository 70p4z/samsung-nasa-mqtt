#!/bin/bash
cd $(dirname $(readlink -f $0))
while [ true ]
do 
	#bash run.sh --mqtt-host host.docker.internal --mqtt-username USERNAME --mqtt-password PASSWORD #--mqtt-tls
	bash run.sh --mqtt-host host.docker.internal --mqtt-username user --mqtt-password user
	sleep 1 
done
