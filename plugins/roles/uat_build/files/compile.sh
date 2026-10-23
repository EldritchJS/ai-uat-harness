#!/bin/bash
# Assisted by Claude Opus
set -euo pipefail
exec 2>&1

test="$1" hash="$2" ginkgo="$3" root="$4"
export HOME=/tmp
export GOCACHE=/tmp/go-cache
export GOPATH=/tmp/gopath
export CGO_ENABLED=0
export PATH="$PATH:$GOPATH/bin"
export GOMAXPROCS=8
export GOFLAGS="-p=8"

dir="$root/$test/$hash"
if [ -x "$dir/test.bin" ]; then
  echo "CACHED $test $hash"
  exit 0
fi
echo "Building $test ($hash)"
work="$(mktemp -d)"
cp "/src/$test.go" "$work/test_test.go"
cp /src/cluster.yaml "$work/cluster.yaml"
cd "$work"
go mod init test
go mod edit -require "github.com/onsi/ginkgo/v2@$ginkgo"
go mod tidy
go run github.com/onsi/ginkgo/v2/ginkgo build -r .
mkdir -p "$dir.tmp"
mv ./*.test "$dir.tmp/test.bin"
cp cluster.yaml "$dir.tmp/cluster.yaml"
chmod +x "$dir.tmp/test.bin"
rm -rf "$dir"
mv "$dir.tmp" "$dir"
cd /
rm -rf "$work"
echo "BUILT $test $hash"
