FROM debian:jessie

## Set the apt source
COPY sources.list /etc/apt/sources.list
RUN apt-get clean && apt-get update

## Pre-install the fundamental packages
RUN apt-get -y install                  \
    rsyslog                             \
    vim-tiny                            \
    python

COPY rsyslog.conf /etc/rsyslog.conf

RUN apt-get -y purge                    \
    exim4                               \
    exim4-base                          \
    exim4-config                        \
    exim4-daemon-light

## Clean up
RUN apt-get clean -y; apt-get autoclean -y; apt-get autoremove -y
RUN rm -rf /usr/share/doc/* /usr/share/locale/* /var/lib/apt/lists/* /tmp/*
