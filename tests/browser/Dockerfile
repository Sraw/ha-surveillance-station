FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN sed -i 's/^Components: main restricted$/Components: main restricted multiverse/; s/^Components: main restricted universe$/Components: main restricted universe multiverse/' /etc/apt/sources.list.d/ubuntu.sources \
 && apt-get update && apt-get install -y --no-install-recommends wget ca-certificates gnupg libpci3 weston \
      intel-media-va-driver-non-free vainfo nodejs fonts-dejavu-core dbus \
 && wget -q -O /tmp/chrome.deb https://dl.google.com/linux/direct/google-chrome-stable_current_amd64.deb \
 && apt-get install -y /tmp/chrome.deb && rm /tmp/chrome.deb && rm -rf /var/lib/apt/lists/*
COPY entry.sh /entry.sh
ENTRYPOINT ["/bin/bash", "/entry.sh"]
