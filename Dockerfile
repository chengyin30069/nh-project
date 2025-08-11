FROM alpine:latest

RUN apk update && \
    apk add --no-cache bash aria2 wget procps zip

WORKDIR /nh-project

COPY . .

RUN sed -i 's/\r$//' *.*

RUN chmod +x *.sh

CMD ["/nh-project/download.sh", "nhentai.txt"]