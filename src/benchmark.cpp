#include <algorithm>
#include <atomic>
#include <barrier>
#include <chrono>
#include <cmath>
#include <cstdint>
#include <cstdlib>
#include <fstream>
#include <iomanip>
#include <iostream>
#include <limits>
#include <memory>
#include <mutex>
#include <random>
#include <stdexcept>
#include <string>
#include <thread>
#include <ctime>
#include <vector>

#ifdef BENCH_ROCKSDB
#include <rocksdb/cache.h>
#include <rocksdb/db.h>
#include <rocksdb/options.h>
#include <rocksdb/table.h>
#endif

#ifdef BENCH_WIREDTIGER
#include <wiredtiger.h>
#endif

namespace {

using Clock = std::chrono::steady_clock;

struct Config {
    std::string db_path;
    std::string metrics_path;
    std::string progress_path;
    int warmup_sec = 0;
    int duration_sec = 1800;
    int threads = 4;
    uint64_t key_count = 500000;
    size_t value_size = 100;
    int read_percent = 50;
    double alpha = 0.0;
    uint64_t seed = 20260927;
    int cache_mb = 512;
    int checkpoint_sec = 15;
    int progress_sec = 5;
};

struct Counters {
    std::atomic<uint64_t> ops{0};
    std::atomic<uint64_t> reads{0};
    std::atomic<uint64_t> updates{0};
    std::atomic<uint64_t> errors{0};
};

std::string arg_value(int argc, char **argv, const std::string &name, const std::string &fallback) {
    const std::string prefix = name + "=";
    for (int i = 1; i < argc; ++i) {
        const std::string arg(argv[i]);
        if (arg.rfind(prefix, 0) == 0) {
            return arg.substr(prefix.size());
        }
    }
    return fallback;
}

int arg_int(int argc, char **argv, const std::string &name, int fallback) {
    return std::stoi(arg_value(argc, argv, name, std::to_string(fallback)));
}

uint64_t arg_u64(int argc, char **argv, const std::string &name, uint64_t fallback) {
    return std::stoull(arg_value(argc, argv, name, std::to_string(fallback)));
}

double arg_double(int argc, char **argv, const std::string &name, double fallback) {
    return std::stod(arg_value(argc, argv, name, std::to_string(fallback)));
}

Config parse_args(int argc, char **argv) {
    Config c;
    c.db_path = arg_value(argc, argv, "--db", "./exp_db");
    c.metrics_path = arg_value(argc, argv, "--metrics", "./metrics.csv");
    c.progress_path = arg_value(argc, argv, "--progress", "./progress.csv");
    c.warmup_sec = arg_int(argc, argv, "--warmup", c.warmup_sec);
    c.duration_sec = arg_int(argc, argv, "--duration", c.duration_sec);
    c.threads = arg_int(argc, argv, "--threads", c.threads);
    c.key_count = arg_u64(argc, argv, "--keys", c.key_count);
    c.value_size = static_cast<size_t>(arg_u64(argc, argv, "--value-size", c.value_size));
    c.read_percent = arg_int(argc, argv, "--read-percent", c.read_percent);
    c.alpha = arg_double(argc, argv, "--alpha", c.alpha);
    c.seed = arg_u64(argc, argv, "--seed", c.seed);
    c.cache_mb = arg_int(argc, argv, "--cache-mb", c.cache_mb);
    c.checkpoint_sec = arg_int(argc, argv, "--checkpoint-sec", c.checkpoint_sec);
    c.progress_sec = arg_int(argc, argv, "--progress-sec", c.progress_sec);

    if (c.warmup_sec < 0 || c.threads < 1 || c.duration_sec < 1 || c.key_count < 1 ||
        c.value_size < 1 || c.read_percent < 0 || c.read_percent > 100 ||
        c.alpha < 0.0 || c.cache_mb < 1 || c.checkpoint_sec < 1 ||
        c.progress_sec < 1) {
        throw std::runtime_error("invalid benchmark configuration");
    }
    if (c.alpha > 0.0 && c.alpha < 0.5) {
        throw std::runtime_error("alpha must be 0, or a value large enough for a stable finite Zipf CDF");
    }
    return c;
}

class ZipfSampler {
public:
    ZipfSampler(uint64_t n, double alpha, uint64_t seed) : n_(n), alpha_(alpha), rng_(seed) {
        if (alpha_ > 0.0) {
            cdf_.resize(n_);
            long double sum = 0.0L;
            for (uint64_t i = 1; i <= n_; ++i) {
                sum += 1.0L / std::pow(static_cast<long double>(i), alpha_);
            }
            long double cumulative = 0.0L;
            for (uint64_t i = 1; i <= n_; ++i) {
                cumulative += (1.0L / std::pow(static_cast<long double>(i), alpha_)) / sum;
                cdf_[i - 1] = static_cast<double>(cumulative);
            }
            cdf_.back() = 1.0;
        }
        uniform_index_ = std::uniform_int_distribution<uint64_t>(0, n_ - 1);
        uniform_real_ = std::uniform_real_distribution<double>(0.0, 1.0);
    }

    uint64_t next() {
        if (alpha_ == 0.0) {
            return uniform_index_(rng_);
        }
        const double u = uniform_real_(rng_);
        const auto it = std::lower_bound(cdf_.begin(), cdf_.end(), u);
        return static_cast<uint64_t>(std::distance(cdf_.begin(), it));
    }

private:
    uint64_t n_;
    double alpha_;
    std::mt19937_64 rng_;
    std::uniform_int_distribution<uint64_t> uniform_index_;
    std::uniform_real_distribution<double> uniform_real_;
    std::vector<double> cdf_;
};

std::string make_key(uint64_t id) {
    std::string key = std::to_string(id);
    if (key.size() < 8) {
        key.insert(key.begin(), 8 - key.size(), '0');
    }
    return key;
}

std::string make_value(size_t size) {
    return std::string(size, 'v');
}

double wall_unix_seconds() {
    return std::chrono::duration<double>(
               std::chrono::system_clock::now().time_since_epoch())
        .count();
}

double cpu_seconds() {
    struct timespec ts {};
    if (clock_gettime(CLOCK_PROCESS_CPUTIME_ID, &ts) != 0) {
        return 0.0;
    }
    return static_cast<double>(ts.tv_sec) + static_cast<double>(ts.tv_nsec) / 1e9;
}

void write_progress_header(std::ofstream &out) {
    out << "unix_time,elapsed_sec,ops,reads,updates,errors\n";
}

void progress_loop(const Config &cfg, const Counters &counters, Clock::time_point start,
                   std::atomic<bool> &stop_flag) {
    std::ofstream out(cfg.progress_path, std::ios::out | std::ios::trunc);
    if (!out) {
        throw std::runtime_error("cannot open progress file: " + cfg.progress_path);
    }
    write_progress_header(out);

    while (!stop_flag.load(std::memory_order_relaxed)) {
        std::this_thread::sleep_for(std::chrono::seconds(cfg.progress_sec));
        const double elapsed = std::chrono::duration<double>(Clock::now() - start).count();
        out << std::fixed << std::setprecision(3)
            << wall_unix_seconds() << ',' << elapsed << ','
            << counters.ops.load(std::memory_order_relaxed) << ','
            << counters.reads.load(std::memory_order_relaxed) << ','
            << counters.updates.load(std::memory_order_relaxed) << ','
            << counters.errors.load(std::memory_order_relaxed) << '\n';
        out.flush();
    }

    const double elapsed = std::chrono::duration<double>(Clock::now() - start).count();
    out << std::fixed << std::setprecision(3)
        << wall_unix_seconds() << ',' << elapsed << ','
        << counters.ops.load(std::memory_order_relaxed) << ','
        << counters.reads.load(std::memory_order_relaxed) << ','
        << counters.updates.load(std::memory_order_relaxed) << ','
        << counters.errors.load(std::memory_order_relaxed) << '\n';
}

struct RunResult {
    double wall_sec = 0.0;
    double cpu_sec = 0.0;
    double start_unix = 0.0;
    double end_unix = 0.0;
    uint64_t ops = 0;
    uint64_t reads = 0;
    uint64_t updates = 0;
    uint64_t errors = 0;
};

#ifdef BENCH_ROCKSDB

class RocksRunner {
public:
    explicit RocksRunner(const Config &cfg) : cfg_(cfg) {}

    void populate() {
        rocksdb::Options options;
        options.create_if_missing = true;
        options.compression = rocksdb::kNoCompression;

        rocksdb::BlockBasedTableOptions table_options;
        table_options.block_cache = rocksdb::NewLRUCache(static_cast<size_t>(cfg_.cache_mb) * 1024 * 1024);
        options.table_factory.reset(rocksdb::NewBlockBasedTableFactory(table_options));

        const rocksdb::Status status = rocksdb::DB::Open(options, cfg_.db_path, &db_);
        if (!status.ok()) {
            throw std::runtime_error("RocksDB open failed: " + status.ToString());
        }

        const std::string value = make_value(cfg_.value_size);
        rocksdb::WriteOptions write_options;
        write_options.disableWAL = false;
        write_options.sync = false;

        for (uint64_t i = 0; i < cfg_.key_count; ++i) {
            const std::string key = make_key(i);
            const rocksdb::Status s = db_->Put(write_options, key, value);
            if (!s.ok()) {
                throw std::runtime_error("RocksDB populate failed: " + s.ToString());
            }
        }

        rocksdb::FlushOptions flush_options;
        flush_options.wait = true;
        const rocksdb::Status flush_status = db_->Flush(flush_options);
        if (!flush_status.ok()) {
            throw std::runtime_error("RocksDB flush failed: " + flush_status.ToString());
        }

        rocksdb::CompactRangeOptions compact_options;
        compact_options.exclusive_manual_compaction = false;
        const rocksdb::Status compact_status = db_->CompactRange(compact_options, nullptr, nullptr);
        if (!compact_status.ok()) {
            throw std::runtime_error("RocksDB compact failed: " + compact_status.ToString());
        }
    }

        RunResult run(Counters &counters, std::atomic<bool> &stop_flag) {
        const std::string value = make_value(cfg_.value_size);
        std::vector<std::thread> workers;
        std::barrier start_barrier(cfg_.threads + 1);
        std::barrier warmup_barrier(cfg_.threads + 1);
        std::barrier measurement_barrier(cfg_.threads + 1);

        Clock::time_point warmup_start{};
        Clock::time_point measurement_start{};
        workers.reserve(cfg_.threads);

        for (int tid = 0; tid < cfg_.threads; ++tid) {
            workers.emplace_back([&, tid]() {
                bool passed_start_barrier = false;
                bool passed_warmup_barrier = false;
                bool passed_measurement_barrier = false;

                try {
                    const uint64_t thread_seed =
                        cfg_.seed + 0x9E3779B97F4A7C15ULL * static_cast<uint64_t>(tid + 1);

                    ZipfSampler sampler(cfg_.key_count, cfg_.alpha, thread_seed);
                    std::mt19937_64 rng(thread_seed ^ 0xD1B54A32D192ED03ULL);
                    std::uniform_int_distribution<int> op_dist(0, 99);

                    rocksdb::ReadOptions read_options;
                    rocksdb::WriteOptions write_options;
                    write_options.disableWAL = false;
                    write_options.sync = false;

                    start_barrier.arrive_and_wait();
                    passed_start_barrier = true;

                    while (!stop_flag.load(std::memory_order_relaxed)) {
                        const double elapsed =
                            std::chrono::duration<double>(Clock::now() - warmup_start).count();

                        if (elapsed >= cfg_.warmup_sec) {
                            break;
                        }

                        const std::string key = make_key(sampler.next());

                        if (op_dist(rng) < cfg_.read_percent) {
                            std::string result;
                            const auto status = db_->Get(read_options, key, &result);
                            if (!status.ok() && !status.IsNotFound()) {
                                counters.errors.fetch_add(1, std::memory_order_relaxed);
                            }
                        } else {
                            const auto status = db_->Put(write_options, key, value);
                            if (!status.ok()) {
                                counters.errors.fetch_add(1, std::memory_order_relaxed);
                            }
                        }
                    }

                    warmup_barrier.arrive_and_wait();
                    passed_warmup_barrier = true;

                    measurement_barrier.arrive_and_wait();
                    passed_measurement_barrier = true;

                    while (!stop_flag.load(std::memory_order_relaxed)) {
                        const double elapsed =
                            std::chrono::duration<double>(Clock::now() - measurement_start).count();

                        if (elapsed >= cfg_.duration_sec) {
                            break;
                        }

                        const std::string key = make_key(sampler.next());

                        if (op_dist(rng) < cfg_.read_percent) {
                            std::string result;
                            const auto status = db_->Get(read_options, key, &result);
                            if (!status.ok() && !status.IsNotFound()) {
                                counters.errors.fetch_add(1, std::memory_order_relaxed);
                            }
                            counters.reads.fetch_add(1, std::memory_order_relaxed);
                        } else {
                            const auto status = db_->Put(write_options, key, value);
                            if (!status.ok()) {
                                counters.errors.fetch_add(1, std::memory_order_relaxed);
                            }
                            counters.updates.fetch_add(1, std::memory_order_relaxed);
                        }

                        counters.ops.fetch_add(1, std::memory_order_relaxed);
                    }
                } catch (...) {
                    counters.errors.fetch_add(1, std::memory_order_relaxed);
                    stop_flag.store(true, std::memory_order_relaxed);

                    if (!passed_start_barrier) {
                        start_barrier.arrive_and_drop();
                    }
                    if (!passed_warmup_barrier) {
                        warmup_barrier.arrive_and_drop();
                    }
                    if (!passed_measurement_barrier) {
                        measurement_barrier.arrive_and_drop();
                    }
                }
            });
        }

        warmup_start = Clock::now();
        start_barrier.arrive_and_wait();

        warmup_barrier.arrive_and_wait();

        counters.ops.store(0, std::memory_order_relaxed);
        counters.reads.store(0, std::memory_order_relaxed);
        counters.updates.store(0, std::memory_order_relaxed);
        counters.errors.store(0, std::memory_order_relaxed);

        const double cpu_start = cpu_seconds();
        const double start_unix = wall_unix_seconds();
        measurement_start = Clock::now();

        std::thread progress_thread([&]() {
            progress_loop(cfg_, counters, measurement_start, stop_flag);
        });

        measurement_barrier.arrive_and_wait();

        for (auto &worker : workers) {
            worker.join();
        }

        const auto finish = Clock::now();
        const double cpu_end = cpu_seconds();
        const double end_unix = wall_unix_seconds();

        stop_flag.store(true, std::memory_order_relaxed);
        progress_thread.join();

        RunResult result;
        result.wall_sec =
            std::chrono::duration<double>(finish - measurement_start).count();
        result.cpu_sec = std::max(0.0, cpu_end - cpu_start);
        result.start_unix = start_unix;
        result.end_unix = end_unix;
        result.ops = counters.ops.load(std::memory_order_relaxed);
        result.reads = counters.reads.load(std::memory_order_relaxed);
        result.updates = counters.updates.load(std::memory_order_relaxed);
        result.errors = counters.errors.load(std::memory_order_relaxed);

        return result;
    }

    void close() {
        db_.reset();
    }

private:
    Config cfg_;
    std::unique_ptr<rocksdb::DB> db_;
};

#endif

#ifdef BENCH_WIREDTIGER

class WiredTigerRunner {
public:
    explicit WiredTigerRunner(const Config &cfg) : cfg_(cfg) {}

    void populate() {
        const std::string conn_config =
            "create,cache_size=" + std::to_string(cfg_.cache_mb) + "MB," +
            "log=(enabled=true,remove=true)," +
            "checkpoint=(wait=" + std::to_string(cfg_.checkpoint_sec) + ")";

        check(wiredtiger_open(cfg_.db_path.c_str(), nullptr, conn_config.c_str(), &conn_), "wiredtiger_open");

        WT_SESSION *session = nullptr;
        check(conn_->open_session(conn_, nullptr, nullptr, &session), "open_session");
        const char *table_config = "key_format=S,value_format=S,type=file,exclusive=true";
        check(session->create(session, "table:kv", table_config), "create table");

        WT_CURSOR *cursor = nullptr;
        check(session->open_cursor(session, "table:kv", nullptr, nullptr, &cursor), "open populate cursor");

        const std::string value = make_value(cfg_.value_size);
        for (uint64_t i = 0; i < cfg_.key_count; ++i) {
            const std::string key = make_key(i);
            cursor->set_key(cursor, key.c_str());
            cursor->set_value(cursor, value.c_str());
            check(cursor->insert(cursor), "populate insert");
            check(cursor->reset(cursor), "populate cursor reset");
        }

        check(session->checkpoint(session, nullptr), "populate checkpoint");
        check(cursor->close(cursor), "close populate cursor");
        check(session->close(session, nullptr), "close populate session");
    }

        RunResult run(Counters &counters, std::atomic<bool> &stop_flag) {
        const std::string value = make_value(cfg_.value_size);
        std::vector<std::thread> workers;
        std::barrier start_barrier(cfg_.threads + 1);
        std::barrier warmup_barrier(cfg_.threads + 1);
        std::barrier measurement_barrier(cfg_.threads + 1);

        Clock::time_point warmup_start{};
        Clock::time_point measurement_start{};
        workers.reserve(cfg_.threads);

        for (int tid = 0; tid < cfg_.threads; ++tid) {
            workers.emplace_back([&, tid]() {
                bool passed_start_barrier = false;
                bool passed_warmup_barrier = false;
                bool passed_measurement_barrier = false;

                WT_SESSION *session = nullptr;
                WT_CURSOR *cursor = nullptr;

                try {
                    check(conn_->open_session(conn_, nullptr, nullptr, &session),
                          "worker open session");
                    check(session->open_cursor(
                              session, "table:kv", nullptr, nullptr, &cursor),
                          "worker open cursor");

                    const uint64_t thread_seed =
                        cfg_.seed + 0x9E3779B97F4A7C15ULL * static_cast<uint64_t>(tid + 1);

                    ZipfSampler sampler(cfg_.key_count, cfg_.alpha, thread_seed);
                    std::mt19937_64 rng(thread_seed ^ 0xD1B54A32D192ED03ULL);
                    std::uniform_int_distribution<int> op_dist(0, 99);

                    start_barrier.arrive_and_wait();
                    passed_start_barrier = true;

                    while (!stop_flag.load(std::memory_order_relaxed)) {
                        const double elapsed =
                            std::chrono::duration<double>(Clock::now() - warmup_start).count();

                        if (elapsed >= cfg_.warmup_sec) {
                            break;
                        }

                        const std::string key = make_key(sampler.next());
                        cursor->set_key(cursor, key.c_str());

                        if (op_dist(rng) < cfg_.read_percent) {
                            const int ret = cursor->search(cursor);
                            if (ret != 0 && ret != WT_NOTFOUND) {
                                counters.errors.fetch_add(1, std::memory_order_relaxed);
                            }
                            check(cursor->reset(cursor), "read cursor reset");
                        } else {
                            cursor->set_value(cursor, value.c_str());
                            const int ret = cursor->update(cursor);
                            if (ret != 0) {
                                counters.errors.fetch_add(1, std::memory_order_relaxed);
                            }
                            check(cursor->reset(cursor), "update cursor reset");
                        }
                    }

                    warmup_barrier.arrive_and_wait();
                    passed_warmup_barrier = true;

                    measurement_barrier.arrive_and_wait();
                    passed_measurement_barrier = true;

                    while (!stop_flag.load(std::memory_order_relaxed)) {
                        const double elapsed =
                            std::chrono::duration<double>(Clock::now() - measurement_start).count();

                        if (elapsed >= cfg_.duration_sec) {
                            break;
                        }

                        const std::string key = make_key(sampler.next());
                        cursor->set_key(cursor, key.c_str());

                        if (op_dist(rng) < cfg_.read_percent) {
                            const int ret = cursor->search(cursor);
                            if (ret != 0 && ret != WT_NOTFOUND) {
                                counters.errors.fetch_add(1, std::memory_order_relaxed);
                            }
                            check(cursor->reset(cursor), "read cursor reset");
                            counters.reads.fetch_add(1, std::memory_order_relaxed);
                        } else {
                            cursor->set_value(cursor, value.c_str());
                            const int ret = cursor->update(cursor);
                            if (ret != 0) {
                                counters.errors.fetch_add(1, std::memory_order_relaxed);
                            }
                            check(cursor->reset(cursor), "update cursor reset");
                            counters.updates.fetch_add(1, std::memory_order_relaxed);
                        }

                        counters.ops.fetch_add(1, std::memory_order_relaxed);
                    }
                } catch (...) {
                    counters.errors.fetch_add(1, std::memory_order_relaxed);
                    stop_flag.store(true, std::memory_order_relaxed);

                    if (!passed_start_barrier) {
                        start_barrier.arrive_and_drop();
                    }
                    if (!passed_warmup_barrier) {
                        warmup_barrier.arrive_and_drop();
                    }
                    if (!passed_measurement_barrier) {
                        measurement_barrier.arrive_and_drop();
                    }
                }

                if (cursor != nullptr) {
                    cursor->close(cursor);
                }
                if (session != nullptr) {
                    session->close(session, nullptr);
                }
            });
        }

        warmup_start = Clock::now();
        start_barrier.arrive_and_wait();

        warmup_barrier.arrive_and_wait();

        counters.ops.store(0, std::memory_order_relaxed);
        counters.reads.store(0, std::memory_order_relaxed);
        counters.updates.store(0, std::memory_order_relaxed);
        counters.errors.store(0, std::memory_order_relaxed);

        const double cpu_start = cpu_seconds();
        const double start_unix = wall_unix_seconds();
        measurement_start = Clock::now();

        std::thread progress_thread([&]() {
            progress_loop(cfg_, counters, measurement_start, stop_flag);
        });

        measurement_barrier.arrive_and_wait();

        for (auto &worker : workers) {
            worker.join();
        }

        const auto finish = Clock::now();
        const double cpu_end = cpu_seconds();
        const double end_unix = wall_unix_seconds();

        stop_flag.store(true, std::memory_order_relaxed);
        progress_thread.join();

        RunResult result;
        result.wall_sec =
            std::chrono::duration<double>(finish - measurement_start).count();
        result.cpu_sec = std::max(0.0, cpu_end - cpu_start);
        result.start_unix = start_unix;
        result.end_unix = end_unix;
        result.ops = counters.ops.load(std::memory_order_relaxed);
        result.reads = counters.reads.load(std::memory_order_relaxed);
        result.updates = counters.updates.load(std::memory_order_relaxed);
        result.errors = counters.errors.load(std::memory_order_relaxed);

        return result;
    }

    void close() {
        if (conn_ != nullptr) {
            conn_->close(conn_, nullptr);
            conn_ = nullptr;
        }
    }

private:
    static void check(int ret, const char *what) {
        if (ret != 0) {
#ifdef BENCH_WIREDTIGER
            throw std::runtime_error(std::string(what) + ": " + wiredtiger_strerror(ret));
#else
            throw std::runtime_error(what);
#endif
        }
    }

    Config cfg_;
    WT_CONNECTION *conn_ = nullptr;
};

#endif

} // namespace

int main(int argc, char **argv) {
    try {
        const Config cfg = parse_args(argc, argv);

#if defined(BENCH_ROCKSDB) && defined(BENCH_WIREDTIGER)
#error "Build exactly one engine per executable"
#endif

#if !defined(BENCH_ROCKSDB) && !defined(BENCH_WIREDTIGER)
#error "Build with BENCH_ROCKSDB or BENCH_WIREDTIGER"
#endif

#ifdef BENCH_ROCKSDB
        RocksRunner runner(cfg);
        const char *engine = "rocksdb";
#else
        WiredTigerRunner runner(cfg);
        const char *engine = "wiredtiger";
#endif

        std::cout << "Preparing " << engine << "...\n";
        runner.populate();
        std::this_thread::sleep_for(std::chrono::seconds(5));

        Counters counters;
        std::atomic<bool> stop_flag{false};

        std::cout << "Warm-up: " << cfg.warmup_sec
                  << " s | Measurement: " << cfg.duration_sec << " s\n";
        const RunResult result = runner.run(counters, stop_flag);

        const double wall_sec = result.wall_sec;
        const double cpu_sec = result.cpu_sec;
        const double start_unix = result.start_unix;
        const double end_unix = result.end_unix;
        const uint64_t ops = result.ops;
        const uint64_t reads = result.reads;
        const uint64_t updates = result.updates;
        const uint64_t errors = result.errors;
        const double logical_write_bytes = static_cast<double>(updates) * static_cast<double>(cfg.value_size);
        const unsigned hw_threads = std::max(1u, std::thread::hardware_concurrency());
        const double process_cpu_util_pct = (cpu_sec / wall_sec) * 100.0 / static_cast<double>(hw_threads);

        std::ofstream metrics(cfg.metrics_path, std::ios::out | std::ios::trunc);
        if (!metrics) {
            throw std::runtime_error("cannot open metrics file: " + cfg.metrics_path);
        }

        metrics << "engine,db_path,warmup_sec,duration_config_sec,wall_sec,start_unix,end_unix,"
                   "threads,key_count,value_size,read_percent,update_percent,alpha,seed,cache_mb,"
                   "checkpoint_sec,ops,reads,updates,errors,logical_write_bytes,cpu_seconds,"
                   "process_cpu_util_percent\n";

        metrics << engine << ',' << cfg.db_path << ',' << cfg.warmup_sec << ','
                << cfg.duration_sec << ','
                << std::fixed << std::setprecision(6) << wall_sec << ','
                << start_unix << ',' << end_unix << ','
                << cfg.threads << ',' << cfg.key_count << ',' << cfg.value_size << ','
                << cfg.read_percent << ',' << (100 - cfg.read_percent) << ','
                << cfg.alpha << ',' << cfg.seed << ',' << cfg.cache_mb << ','
                << cfg.checkpoint_sec << ',' << ops << ',' << reads << ',' << updates << ','
                << errors << ',' << logical_write_bytes << ',' << cpu_sec << ','
                << process_cpu_util_pct << '\n';
        metrics.close();

        runner.close();
        std::cout << std::fixed << std::setprecision(2)
                  << "Completed: " << ops / wall_sec << " ops/s, "
                  << updates * cfg.value_size / wall_sec / (1024.0 * 1024.0) << " MiB/s logical update data, "
                  << errors << " errors\n";
    } catch (const std::exception &ex) {
        std::cerr << "Benchmark failed: " << ex.what() << '\n';
        return 1;
    }

    return 0;
}
