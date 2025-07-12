FROM alpine:latest

RUN apk update && \
    apk add --no-cache bash wget curl procps zip

WORKDIR /nh-project

COPY . .

RUN sed -i 's/\r$//' *.*

RUN chmod +x *.sh

CMD ["/bin/bash","download.sh", "nhentai.txt"]