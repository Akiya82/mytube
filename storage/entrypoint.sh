#!/bin/sh
# Entrypoint for the `storage` service (chrislusf/seaweedfs:3.97). The image
# has no envsubst (no gettext package), so s3.json.template is substituted
# with sed instead — an equivalent per src/docs/experiments/07-object-storage.md's
# instruction, not a literal envsubst call. Safe here specifically because
# MYTUBE_S3_KEY/MYTUBE_S3_SECRET are generated with `openssl rand -hex`
# (hex only), so they can never contain a sed-special `/` or `&`.
#
# Identity "mytube" gets Admin/Read/Write/List/Tagging; identity "anonymous"
# gets Read only — the sole mechanism SeaweedFS has for the D-38 anonymous
# in-network read that edge depends on (OSS-nginx cannot sign AWS SigV4).
set -eu

sed -e "s/__MYTUBE_S3_KEY__/${MYTUBE_S3_KEY}/" -e "s/__MYTUBE_S3_SECRET__/${MYTUBE_S3_SECRET}/" \
  /etc/seaweedfs/s3.json.template > /tmp/s3.json

exec weed server -dir=/data -s3 -s3.port=8333 -s3.config=/tmp/s3.json -master.volumeSizeLimitMB=1024
