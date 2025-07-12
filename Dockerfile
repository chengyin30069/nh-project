FROM alpine:latest

RUN apk update && \
    apk add --no-cache bash wget curl procps zip

WORKDIR /

COPY *.sh ./

COPY nhentai.txt ./

RUN sed -i 's/\r$//' *.sh

RUN sed -i 's/\r$//' nhentai.txt

RUN chmod +x *.sh

CMD ["/bin/bash","download.sh", "nhentai.txt"]