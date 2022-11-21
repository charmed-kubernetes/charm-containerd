#!/usr/bin/env bash

set -eux
VERSION="${VERSION:-1.6.10}"
ARCH="${ARCH:-amd64 arm64 }"

temp_dir="$(readlink -f build-resources.tmp)"
rm -rf "$temp_dir"
mkdir "$temp_dir"
(cd "$temp_dir"
 for arch in $ARCH; do
    echo "Download containerd release ${VERSION} for ${arch}"
    rel_link="https://github.com/containerd/containerd/releases/download/v${VERSION}/containerd-${VERSION}-linux-${arch}.tar.gz"
    wget $rel_link
 done
)
tar -czvf containerd-multiarch.tgz -C "${temp_dir}" .
rm -rf "$temp_dir"
