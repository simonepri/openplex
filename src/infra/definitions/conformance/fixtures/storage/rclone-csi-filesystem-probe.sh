#!/bin/sh
# Executes file creation, move, and read operations on Rclone CSI mounts to defend filesystem contract compatibility for workload storage.

set -eu

printf home >/s3/eaws-lh1/home/chainsaw-source
mv /s3/eaws-lh1/home/chainsaw-source /s3/eaws-lh1/home/chainsaw-renamed
content=$(cat /s3/eaws-lh1/home/chainsaw-renamed)
test "${content}" = home
rm /s3/eaws-lh1/home/chainsaw-renamed
test ! -e /s3/eaws-lh1/home/chainsaw-renamed
printf global >/s3/global/home/chainsaw-source
mv /s3/global/home/chainsaw-source /s3/global/home/chainsaw-renamed
content=$(cat /s3/global/home/chainsaw-renamed)
test "${content}" = global
rm /s3/global/home/chainsaw-renamed
test ! -e /s3/global/home/chainsaw-renamed
touch /s3/eaws-lh1/scratch/chainsaw-scratch
touch /s3/global/home/chainsaw-global
touch /s3/eaws-lh1/home/chainsaw-restart
