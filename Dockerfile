# https://developers.home-assistant.io/docs/add-ons/configuration#add-on-dockerfile
# arch:
#   - armhf
#   - armv7
#   - aarch64
#   - amd64
#   - i386
ARG BUILD_ARCH=armhf
FROM ghcr.io/home-assistant/${BUILD_ARCH}-base:3.21

# install samsung nasa bridge
RUN apk add socat python3 py3-pip git openssh

RUN mkdir -p /samsung_nasa_link

#RUN git clone https://github.com/70p4z/samsung-nasa-mqtt /samsung_nasa_link 
COPY *.py *.sh requirements.txt *.conf /samsung_nasa_link/

RUN pip install --break-system-packages -r /samsung_nasa_link/requirements.txt
RUN chmod a+x /samsung_nasa_link/startup_docker.sh

CMD [ "/samsung_nasa_link/startup_docker.sh" ]
