#!/bin/sh
# fetch current running arch
BUILD_ARCH=`cat /etc/lfs-release | grep SUPERVISOR_ARCH |cut -f2 -d=`
# build image
docker build --build-arg BUILD_ARCH=$BUILD_ARCH --no-cache -t samsung_nasa_image .
# delete and create privileged container with access to serial ports
docker stop samsung_nasa 2>&1
docker container rm samsung_nasa 2>&1
docker run -d --privileged -v /dev/serial/by-id/:/dev/serial/by-id/ -v /dev:/dev --add-host host.docker.internal:host-gateway --name samsung_nasa samsung_nasa_image
