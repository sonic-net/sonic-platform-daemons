FROM debian:jessie

## Clean documentation in FROM image
RUN find /usr/share/doc -depth \( -type f -o -type l \) ! -name copyright | xargs rm || true
## Clean doc directories that are empty or only contain empty directories
RUN while [ -n "$(find /usr/share/doc -depth -type d -empty -print -exec rmdir {} +)" ]; do :; done
RUN rm -rf                              \
        /usr/share/man/*                \
        /usr/share/groff/*              \
        /usr/share/info/*               \
        /usr/share/lintian/*            \
        /usr/share/linda/*              \
        /var/cache/man/*                \
        /usr/share/locale/*

## Set the apt source
COPY sources.list /etc/apt/sources.list
COPY dpkg_01_drop /etc/dpkg/dpkg.cfg.d/01_drop
RUN apt-get clean && apt-get update

## Pre-install the fundamental packages
RUN apt-get -y install                  \
    rsyslog                             \
    vim-tiny                            \
    perl                                \
    python

COPY rsyslog.conf /etc/rsyslog.conf

RUN apt-get -y purge                    \
    exim4                               \
    exim4-base                          \
    exim4-config                        \
    exim4-daemon-light

## Clean up apt
## Remove /var/lib/apt/lists/*, could be obsoleted for derived images
RUN apt-get clean -y; apt-get autoclean -y; apt-get autoremove -y;      \
    rm -rf /var/lib/apt/lists/*;                                        \
    rm -rf /tmp/*;

