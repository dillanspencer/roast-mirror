FROM balenalib/raspberrypi4-64-debian:latest

# Install necessary packages
RUN install_packages ffmpeg curl bash

# Copy files
COPY ./mediamtx /
COPY ./mediamtx.yml /
COPY ./entry.sh /

RUN chmod +x /entry.sh

ENTRYPOINT ["/bin/sh", "/entry.sh"]