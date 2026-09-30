cmake_minimum_required(VERSION 3.20)
project(storage_engine_bench LANGUAGES CXX)

set(CMAKE_CXX_STANDARD 20)
set(CMAKE_CXX_STANDARD_REQUIRED ON)
set(CMAKE_CXX_EXTENSIONS OFF)

set(ROCKSDB_ROOT "" CACHE PATH "RocksDB source/build root")
set(WIREDTIGER_ROOT "" CACHE PATH "WiredTiger source/build root")

find_path(ROCKSDB_INCLUDE_DIR
    NAMES rocksdb/db.h
    PATHS "${ROCKSDB_ROOT}/include"
    NO_DEFAULT_PATH)

find_library(ROCKSDB_LIBRARY
    NAMES rocksdb
    PATHS "${ROCKSDB_ROOT}/build"
    NO_DEFAULT_PATH)

find_path(WIREDTIGER_INCLUDE_DIR
    NAMES wiredtiger.h
    PATHS
        "${WIREDTIGER_ROOT}/build"
        "${WIREDTIGER_ROOT}/build/include"
        "${WIREDTIGER_ROOT}/src/include"
    NO_DEFAULT_PATH)

find_library(WIREDTIGER_LIBRARY
    NAMES wiredtiger
    PATHS
        "${WIREDTIGER_ROOT}/build"
        "${WIREDTIGER_ROOT}/build/.libs"
        "${WIREDTIGER_ROOT}/lib"
    NO_DEFAULT_PATH)

if(NOT ROCKSDB_INCLUDE_DIR OR NOT ROCKSDB_LIBRARY)
    message(FATAL_ERROR "Could not find RocksDB. Set ROCKSDB_ROOT to the RocksDB source tree.")
endif()

if(NOT WIREDTIGER_INCLUDE_DIR OR NOT WIREDTIGER_LIBRARY)
    message(FATAL_ERROR "Could not find WiredTiger. Set WIREDTIGER_ROOT to the WiredTiger source tree.")
endif()

add_executable(bench_rocksdb src/benchmark.cpp)
target_compile_options(bench_rocksdb PRIVATE -O3 -DNDEBUG)
target_compile_definitions(bench_rocksdb PRIVATE BENCH_ROCKSDB=1)
target_include_directories(bench_rocksdb PRIVATE "${ROCKSDB_INCLUDE_DIR}")
target_link_libraries(bench_rocksdb PRIVATE "${ROCKSDB_LIBRARY}" pthread dl)
set_target_properties(bench_rocksdb PROPERTIES
    BUILD_RPATH "${ROCKSDB_ROOT}/build"
    INSTALL_RPATH "${ROCKSDB_ROOT}/build")


add_executable(bench_wiredtiger src/benchmark.cpp)
target_compile_options(bench_wiredtiger PRIVATE -O3 -DNDEBUG)
target_compile_definitions(bench_wiredtiger PRIVATE BENCH_WIREDTIGER=1)
target_include_directories(bench_wiredtiger PRIVATE "${WIREDTIGER_INCLUDE_DIR}")
target_link_libraries(bench_wiredtiger PRIVATE "${WIREDTIGER_LIBRARY}" pthread dl m)
set_target_properties(bench_wiredtiger PROPERTIES
    BUILD_RPATH "${WIREDTIGER_ROOT}/build;${WIREDTIGER_ROOT}/build/.libs"
    INSTALL_RPATH "${WIREDTIGER_ROOT}/build;${WIREDTIGER_ROOT}/build/.libs")
