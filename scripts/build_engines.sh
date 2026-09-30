#!/usr/bin/env bash
set -euo pipefail

ROOT_DIR="$(cd "$(dirname "$0")/.." && pwd)"
JOBS="${JOBS:-$(nproc)}"
ROCKSDB_REF="${ROCKSDB_REF:-v11.8.1}"
WIREDTIGER_REF="${WIREDTIGER_REF:-11.3.1}"

clone_or_checkout() {
    local url="$1"
    local dir="$2"
    local ref="$3"

    if [[ ! -d "$dir/.git" ]]; then
        git clone --branch "$ref" --depth 1 "$url" "$dir"
        return
    fi

    if ! git -C "$dir" diff --quiet || ! git -C "$dir" diff --cached --quiet; then
        echo "Refusing to modify dirty repository: $dir" >&2
        exit 1
    fi

    git -C "$dir" fetch --tags --force origin
    git -C "$dir" checkout --force "$ref"
}

clone_or_checkout \
    "https://github.com/facebook/rocksdb.git" \
    "$ROOT_DIR/rocksdb" \
    "$ROCKSDB_REF"

clone_or_checkout \
    "https://github.com/wiredtiger/wiredtiger.git" \
    "$ROOT_DIR/wiredtiger" \
    "$WIREDTIGER_REF"

cmake -S "$ROOT_DIR/rocksdb" -B "$ROOT_DIR/rocksdb/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DROCKSDB_BUILD_SHARED=ON \
    -DFAIL_ON_WARNINGS=OFF \
    -DWITH_GFLAGS=OFF \
    -DWITH_LIBURING=OFF \
    -DWITH_SNAPPY=OFF \
    -DWITH_LZ4=OFF \
    -DWITH_ZLIB=OFF \
    -DWITH_BZ2=OFF \
    -DWITH_ZSTD=OFF \
    -DPORTABLE=1
cmake --build "$ROOT_DIR/rocksdb/build" --parallel "$JOBS"

cmake -S "$ROOT_DIR/wiredtiger" -B "$ROOT_DIR/wiredtiger/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DCMAKE_POSITION_INDEPENDENT_CODE=ON \
    -DENABLE_PYTHON=OFF \
    -DENABLE_STRICT=OFF \
    -DCMAKE_C_FLAGS="-w -Wno-error"
cmake --build "$ROOT_DIR/wiredtiger/build" --parallel "$JOBS"

cmake -S "$ROOT_DIR" -B "$ROOT_DIR/build" \
    -DCMAKE_BUILD_TYPE=Release \
    -DROCKSDB_ROOT="$ROOT_DIR/rocksdb" \
    -DWIREDTIGER_ROOT="$ROOT_DIR/wiredtiger"
cmake --build "$ROOT_DIR/build" --parallel "$JOBS"

echo "Built benchmark binaries:"
echo "  $ROOT_DIR/build/bench_rocksdb"
echo "  $ROOT_DIR/build/bench_wiredtiger"
