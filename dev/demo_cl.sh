#!/usr/bin/env bash
# Add a realistic-looking code change to the development p4d, mainly for
# documentation screenshots: a small C++ library is imported first, then a
# multi-file changelist edits several sources, adds one and deletes one.
#
# Usage: dev/demo_cl.sh   (expects the server from dev/seed.sh to be running)
set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
# shellcheck source=env.sh
source "$HERE/env.sh"

WS="$(mktemp -d)"
trap 'rm -rf "$WS"' EXIT
cd "$WS"

# One workspace per user, rooted in the temporary directory.
for u in dave erin; do
    p4 -u "$u" -c "demo-$u" client -o \
        | sed "s#^Root:.*#Root: $WS#" \
        | p4 -u "$u" -c "demo-$u" client -i >/dev/null
done
as() { local user=$1; shift; p4 -u "$user" -c "demo-$user" "$@"; }

P=main/netlib
mkdir -p "$P/include/netlib" "$P/src" "$P/tests"

# ---------------------------------------------------------------------------
# CL A: initial version of the library
# ---------------------------------------------------------------------------
cat > "$P/CMakeLists.txt" <<'EOF'
cmake_minimum_required(VERSION 3.20)
project(netlib CXX)

set(CMAKE_CXX_STANDARD 17)

add_library(netlib
    src/http_client.cpp
    src/logger.cpp
    src/url.cpp
)
target_include_directories(netlib PUBLIC include)

add_executable(netlib_tests tests/test_http_client.cpp)
target_link_libraries(netlib_tests PRIVATE netlib)
EOF

cat > "$P/include/netlib/http_client.h" <<'EOF'
#pragma once

#include <string>
#include <map>

namespace netlib {

struct Response {
    int status = 0;
    std::string body;
    std::map<std::string, std::string> headers;
};

class HttpClient {
public:
    explicit HttpClient(std::string base_url);

    Response get(const std::string& path);
    Response post(const std::string& path, const std::string& body);

    void set_timeout_ms(int timeout_ms);

private:
    Response send(const std::string& method, const std::string& path,
                  const std::string& body);

    std::string base_url_;
    int timeout_ms_ = 5000;
};

}  // namespace netlib
EOF

cat > "$P/src/http_client.cpp" <<'EOF'
#include "netlib/http_client.h"
#include "netlib/logger.h"

#include <stdexcept>

namespace netlib {

HttpClient::HttpClient(std::string base_url)
    : base_url_(std::move(base_url)) {}

Response HttpClient::get(const std::string& path) {
    return send("GET", path, "");
}

Response HttpClient::post(const std::string& path, const std::string& body) {
    return send("POST", path, body);
}

void HttpClient::set_timeout_ms(int timeout_ms) {
    timeout_ms_ = timeout_ms;
}

Response HttpClient::send(const std::string& method, const std::string& path,
                          const std::string& body) {
    log_info(method + " " + base_url_ + path);

    Response response = transport::perform(method, base_url_ + path, body,
                                           timeout_ms_);
    if (response.status >= 500) {
        throw std::runtime_error("server error: " +
                                 std::to_string(response.status));
    }
    return response;
}

}  // namespace netlib
EOF

cat > "$P/include/netlib/logger.h" <<'EOF'
#pragma once

#include <string>

namespace netlib {

void log_info(const std::string& message);
void log_error(const std::string& message);

}  // namespace netlib
EOF

cat > "$P/src/logger.cpp" <<'EOF'
#include "netlib/logger.h"

#include <iostream>

namespace netlib {

void log_info(const std::string& message) {
    std::cout << "[info] " << message << std::endl;
}

void log_error(const std::string& message) {
    std::cerr << "[error] " << message << std::endl;
}

}  // namespace netlib
EOF

cat > "$P/src/url.cpp" <<'EOF'
#include <string>

namespace netlib {

// Legacy helper, superseded by std::string concatenation in HttpClient.
std::string join_url(const std::string& base, const std::string& path) {
    if (!base.empty() && base.back() == '/' && !path.empty() && path[0] == '/')
        return base + path.substr(1);
    return base + path;
}

}  // namespace netlib
EOF

cat > "$P/tests/test_http_client.cpp" <<'EOF'
#include "netlib/http_client.h"

#include <cassert>

int main() {
    netlib::HttpClient client("http://localhost:8080");
    client.set_timeout_ms(1000);

    netlib::Response r = client.get("/health");
    assert(r.status == 200);
    return 0;
}
EOF

as dave add "$P"/CMakeLists.txt "$P"/include/netlib/*.h "$P"/src/*.cpp "$P"/tests/*.cpp >/dev/null
as dave submit -d "netlib: initial HTTP client library" >/dev/null

# ---------------------------------------------------------------------------
# CL B: multi-file feature change (the one to screenshot)
# ---------------------------------------------------------------------------
as erin sync -q >/dev/null
as erin edit "$P"/CMakeLists.txt "$P"/include/netlib/http_client.h "$P"/include/netlib/logger.h \
    "$P"/src/http_client.cpp "$P"/src/logger.cpp "$P"/tests/test_http_client.cpp >/dev/null
as erin delete "$P"/src/url.cpp >/dev/null

cat > "$P/include/netlib/logger.h" <<'EOF'
#pragma once

#include <string>

namespace netlib {

void log_info(const std::string& message);
void log_warning(const std::string& message);
void log_error(const std::string& message);

}  // namespace netlib
EOF

cat > "$P/CMakeLists.txt" <<'EOF'
cmake_minimum_required(VERSION 3.20)
project(netlib CXX)

set(CMAKE_CXX_STANDARD 17)

add_library(netlib
    src/http_client.cpp
    src/logger.cpp
    src/retry_policy.cpp
)
target_include_directories(netlib PUBLIC include)

add_executable(netlib_tests tests/test_http_client.cpp)
target_link_libraries(netlib_tests PRIVATE netlib)
enable_testing()
add_test(NAME netlib_tests COMMAND netlib_tests)
EOF

cat > "$P/include/netlib/http_client.h" <<'EOF'
#pragma once

#include "netlib/retry_policy.h"

#include <string>
#include <map>

namespace netlib {

struct Response {
    int status = 0;
    std::string body;
    std::map<std::string, std::string> headers;

    bool ok() const { return status >= 200 && status < 300; }
};

class HttpClient {
public:
    explicit HttpClient(std::string base_url, RetryPolicy retry = {});

    Response get(const std::string& path);
    Response post(const std::string& path, const std::string& body);

    void set_timeout_ms(int timeout_ms);
    void set_retry_policy(RetryPolicy retry);

private:
    Response send(const std::string& method, const std::string& path,
                  const std::string& body);
    Response send_once(const std::string& method, const std::string& url,
                       const std::string& body);

    std::string base_url_;
    int timeout_ms_ = 5000;
    RetryPolicy retry_;
};

}  // namespace netlib
EOF

cat > "$P/src/http_client.cpp" <<'EOF'
#include "netlib/http_client.h"
#include "netlib/logger.h"

#include <chrono>
#include <stdexcept>
#include <thread>

namespace netlib {

HttpClient::HttpClient(std::string base_url, RetryPolicy retry)
    : base_url_(std::move(base_url)), retry_(retry) {}

Response HttpClient::get(const std::string& path) {
    return send("GET", path, "");
}

Response HttpClient::post(const std::string& path, const std::string& body) {
    return send("POST", path, body);
}

void HttpClient::set_timeout_ms(int timeout_ms) {
    timeout_ms_ = timeout_ms;
}

void HttpClient::set_retry_policy(RetryPolicy retry) {
    retry_ = retry;
}

Response HttpClient::send(const std::string& method, const std::string& path,
                          const std::string& body) {
    const std::string url = base_url_ + path;

    for (int attempt = 1;; ++attempt) {
        Response response = send_once(method, url, body);
        if (response.status < 500 || attempt >= retry_.max_attempts) {
            return response;
        }
        auto delay = retry_.delay_for(attempt);
        log_warning(method + " " + url + " failed with " +
                    std::to_string(response.status) + ", retrying in " +
                    std::to_string(delay.count()) + " ms");
        std::this_thread::sleep_for(delay);
    }
}

Response HttpClient::send_once(const std::string& method,
                               const std::string& url,
                               const std::string& body) {
    log_info(method + " " + url);
    return transport::perform(method, url, body, timeout_ms_);
}

}  // namespace netlib
EOF

cat > "$P/include/netlib/retry_policy.h" <<'EOF'
#pragma once

#include <chrono>

namespace netlib {

// Exponential backoff: initial_delay, 2x, 4x, ... capped at max_delay.
struct RetryPolicy {
    int max_attempts = 3;
    std::chrono::milliseconds initial_delay{100};
    std::chrono::milliseconds max_delay{2000};

    std::chrono::milliseconds delay_for(int attempt) const;
};

}  // namespace netlib
EOF

cat > "$P/src/retry_policy.cpp" <<'EOF'
#include "netlib/retry_policy.h"

#include <algorithm>

namespace netlib {

std::chrono::milliseconds RetryPolicy::delay_for(int attempt) const {
    auto delay = initial_delay * (1 << std::max(0, attempt - 1));
    return std::min(delay, max_delay);
}

}  // namespace netlib
EOF

cat > "$P/src/logger.cpp" <<'EOF'
#include "netlib/logger.h"

#include <iostream>

namespace netlib {

void log_info(const std::string& message) {
    std::cout << "[info] " << message << std::endl;
}

void log_warning(const std::string& message) {
    std::cerr << "[warn] " << message << std::endl;
}

void log_error(const std::string& message) {
    std::cerr << "[error] " << message << std::endl;
}

}  // namespace netlib
EOF

cat > "$P/tests/test_http_client.cpp" <<'EOF'
#include "netlib/http_client.h"
#include "netlib/retry_policy.h"

#include <cassert>

using namespace std::chrono_literals;

static void test_retry_delays() {
    netlib::RetryPolicy policy;
    policy.initial_delay = 100ms;
    policy.max_delay = 300ms;

    assert(policy.delay_for(1) == 100ms);
    assert(policy.delay_for(2) == 200ms);
    assert(policy.delay_for(3) == 300ms);  // capped
}

static void test_health() {
    netlib::HttpClient client("http://localhost:8080");
    client.set_timeout_ms(1000);

    netlib::Response r = client.get("/health");
    assert(r.ok());
}

int main() {
    test_retry_delays();
    test_health();
    return 0;
}
EOF

as erin add "$P"/include/netlib/retry_policy.h "$P"/src/retry_policy.cpp >/dev/null
as erin submit -d "netlib: retry failed requests with exponential backoff

HttpClient now retries 5xx responses according to a RetryPolicy
(3 attempts, 100 ms initial delay, capped at 2 s) instead of throwing.

- Add RetryPolicy with delay_for() and unit tests
- Add Response::ok() and log_warning()
- Remove unused url.cpp (join_url was dead code)" >/dev/null

echo "Demo changelists added; newest is $(p4 changes -m1 -s submitted | cut -d' ' -f2)"
