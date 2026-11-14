# A company laptop image: base tools installed, and the packages the acme-api snags need downloaded but not
# installed, so every snag still fails the same way and only its fix skips the download.
FROM ubuntu:22.04
ENV DEBIAN_FRONTEND=noninteractive
RUN rm /etc/apt/apt.conf.d/docker-clean \
 && apt-get update \
 && apt-get install -y --no-install-recommends git openssh-client curl ca-certificates python3 python3-venv python3-pip \
 && apt-get install -y --download-only software-properties-common \
    build-essential gcc libpq-dev python3-dev libldap2-dev libsasl2-dev \
 && apt-get install -y software-properties-common \
 && add-apt-repository -y ppa:deadsnakes/ppa \
 && apt-get install -y --download-only python3.12 python3.12-venv python3.12-dev \
 && add-apt-repository -y --remove ppa:deadsnakes/ppa \
 && rm -f /etc/apt/trusted.gpg.d/deadsnakes* /etc/apt/sources.list.d/deadsnakes* /var/lib/apt/lists/*deadsnakes* \
 && apt-get purge -y --auto-remove software-properties-common
