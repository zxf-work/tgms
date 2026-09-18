#!/bin/bash
set -x
echo "RUN_STARTED fetch-projected-fk $(date -u +%FT%TZ)"
cd /mnt/project/xzhang/tgms/ldbc-sf1/bi-sf1-composite-projected-fk || exit 1
curl -sSLO https://datasets.ldbcouncil.org/bi-pre-audit/bi-sf1-composite-projected-fk.tar.zst || { echo "FETCH_FAIL data"; exit 1; }
curl -sSLO https://datasets.ldbcouncil.org/bi-pre-audit/bi-composite-projected-fk-md5sums.tar.zst || { echo "FETCH_FAIL md5"; exit 1; }
ls -l
echo "DECOMPRESS $(date -u +%FT%TZ)"
zstd -d -f bi-sf1-composite-projected-fk.tar.zst -o bi-sf1-composite-projected-fk.tar || { echo "ZSTD_FAIL"; exit 1; }
tar xf bi-sf1-composite-projected-fk.tar || { echo "TAR_FAIL"; exit 1; }
tar xf bi-composite-projected-fk-md5sums.tar.zst || echo "TAR_MD5_NOTE nonfatal"
echo "MD5CHECK $(date -u +%FT%TZ)"
ls -la
echo "FETCH_DONE $(date -u +%FT%TZ)"
