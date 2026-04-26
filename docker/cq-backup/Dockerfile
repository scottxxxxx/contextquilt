FROM google/cloud-sdk:alpine

RUN apk add --no-cache postgresql15-client tini && \
    rm -rf /var/cache/apk/*

COPY backup.sh /usr/local/bin/cq-backup
COPY entrypoint.sh /usr/local/bin/entrypoint

RUN chmod +x /usr/local/bin/cq-backup /usr/local/bin/entrypoint

ENTRYPOINT ["tini", "--", "/usr/local/bin/entrypoint"]
