FROM python:2.7-alpine
MAINTAINER Tri M. Cao <trimcao@gmail.com>

WORKDIR /usr/src/app

COPY requirements.txt /usr/src/app/
RUN pip install --no-cache-dir -r requirements.txt

COPY cachet_url_monitor/* /usr/src/app/cachet_url_monitor/

COPY config.yml /usr/src/app/config/
COPY atesvc-2016/* /usr/src/app/config/atesvc-2016/
VOLUME /usr/src/app/config/

COPY atesvc-2016.sh .
RUN chmod 755 atesvc-2016.sh
ENTRYPOINT [ "./atesvc-2016.sh" ]
