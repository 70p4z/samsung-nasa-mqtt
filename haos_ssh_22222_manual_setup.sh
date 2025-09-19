#!/bin/sh
BUILD_ARCH=$1
# fetch current running arch
if [ -z "$BUILD_ARCH" ]
then
	BUILD_ARCH=`cat /etc/lfs-release | grep SUPERVISOR_ARCH |cut -f2 -d=`
fi
if [ -z "$BUILD_ARCH" ]
then
	BUILD_ARCH=`cat /etc/os-release | grep SUPERVISOR_ARCH |cut -f2 -d=`
fi
# ask for arch when not provided
if [ -z "$BUILD_ARCH" ]
then
	echo "Please type the architecture to use (amongst armhf, armv7, aarch64, amd64, i386)"
	echo "If you're having troubles with aarch64, please use armhf instead"
	read BUILD_ARCH
fi
if [ -z "$BUILD_ARCH" ]
then
	echo "No valid architecture provided/found"
	echo "possible values: armhf, armv7, aarch64, amd64, i386"
	exit -1
fi

# build image
docker build --build-arg BUILD_ARCH=$BUILD_ARCH --progress=plain --no-cache -t samsung_nasa_image .
# delete and create privileged container with access to serial ports
docker stop samsung_nasa 2>&1
docker container rm samsung_nasa 2>&1
docker run -d --privileged -v /dev/serial/by-id/:/dev/serial/by-id/ -v /dev:/dev -v /tmp:/tmp --add-host host.docker.internal:host-gateway --name samsung_nasa samsung_nasa_image
