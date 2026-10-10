// BridgeSlider - DIVA Mod Loader mod for Hatsune Miku: Project DIVA Mega Mix+.
//
// Connects to a Divaller-protocol bridge server (divaller_bridge.py or
// zhousensor_bridge.py), reads the 24-byte "BVZ" input packets, and publishes
// them into AnySlider's MMIO shared memory as native Arcade Controller input
// (32-cell slider + buttons), the same input the game gets from a HORI NSW-230.
// Optionally drives the controller's slider LEDs with touch feedback.
//
// Requires AnySlider (https://github.com/samnyan/AnySlider) with io_enabled = true.

#define WIN32_LEAN_AND_MEAN
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <timeapi.h>

#include <atomic>
#include <cstdarg>
#include <cstdint>
#include <cstdio>
#include <cstring>
#include <string>
#include <vector>

#include "mm_io/shared_memory.h"
#include "mm_io/shared_memory_v1.h"

namespace mmio = anyslider::mmio;

namespace
{
// ------------------------------------------------------------------ config

struct ButtonBinding
{
    const char* key;    // config key
    int byte;           // byte in the Divaller packet
    int bit;            // bit in that byte
    std::vector<uint32_t> actions;
};

struct Config
{
    std::string host = "127.0.0.1";
    int port = 45710;
    int poll_interval_ms = 1;
    bool slider_reverse = false;
    std::wstring shared_memory = mmio::kSharedMemoryName;
    std::string leds = "touch";         // "touch" or "off"
    uint32_t led_touch_color = 0xFFFFFF;
    uint32_t led_idle_color = 0x000000;
    bool button_leds = true;
    bool debug = false;
    // Divaller packet layout (same as TLAC's Divaller.cpp).
    std::vector<ButtonBinding> buttons = {
        {"button_triangle", 3, 4, {mmio::Triangle}},
        {"button_square",   3, 2, {mmio::Square}},
        {"button_cross",    3, 5, {mmio::Cross}},
        {"button_circle",   3, 3, {mmio::Circle}},
        {"button_fn",       3, 1, {mmio::Start}},
        {"button_l1",       5, 0, {}},
        {"button_l2",       4, 7, {}},
        {"button_l3",       4, 6, {}},
        // Extended buttons, bytes 24-25 of the 'X' reply (diva_bridge.py).
        {"button_dpad_up",    24, 0, {mmio::DpadUp}},
        {"button_dpad_down",  24, 1, {mmio::DpadDown}},
        {"button_dpad_left",  24, 2, {mmio::DpadLeft}},
        {"button_dpad_right", 24, 3, {mmio::DpadRight}},
        {"button_lb",         24, 4, {mmio::L1}},
        {"button_rb",         24, 5, {mmio::R1}},
        {"button_lt",         24, 6, {mmio::L2}},
        {"button_rt",         24, 7, {mmio::R2}},
        {"button_ls",         25, 0, {mmio::L3}},
        {"button_rs",         25, 1, {mmio::R3}},
    };
};

Config g_config;
std::wstring g_modDir;
FILE* g_log = nullptr;
std::atomic_bool g_running{false};

void Log(const char* fmt, ...)
{
    char buf[512];
    va_list args;
    va_start(args, fmt);
    vsnprintf(buf, sizeof(buf), fmt, args);
    va_end(args);
    SYSTEMTIME t;
    GetLocalTime(&t);
    std::printf("[BridgeSlider] %s\n", buf);
    std::fflush(stdout);
    if (g_log)
    {
        std::fprintf(g_log, "%02d:%02d:%02d.%03d %s\n", t.wHour, t.wMinute, t.wSecond, t.wMilliseconds, buf);
        std::fflush(g_log);
    }
}

std::string Trim(const std::string& s)
{
    size_t a = s.find_first_not_of(" \t\r\n");
    size_t b = s.find_last_not_of(" \t\r\n");
    return a == std::string::npos ? std::string() : s.substr(a, b - a + 1);
}

std::string Lower(std::string s)
{
    for (char& c : s)
        c = static_cast<char>(tolower(static_cast<unsigned char>(c)));
    return s;
}

// Strip quotes and TOML escapes from a scalar string value.
std::string Unquote(const std::string& v)
{
    if (v.size() >= 2 && (v.front() == '"' || v.front() == '\''))
    {
        std::string inner = v.substr(1, v.size() - 2);
        if (v.front() == '\'')
            return inner;
        std::string out;
        for (size_t i = 0; i < inner.size(); ++i)
        {
            if (inner[i] == '\\' && i + 1 < inner.size())
                ++i;
            out += inner[i];
        }
        return out;
    }
    return v;
}

// Remove a trailing "# comment" that is outside quotes.
std::string StripComment(const std::string& line)
{
    bool inQuote = false;
    char quote = 0;
    for (size_t i = 0; i < line.size(); ++i)
    {
        char c = line[i];
        if (inQuote)
        {
            if (c == '\\' && quote == '"')
                ++i;
            else if (c == quote)
                inQuote = false;
        }
        else if (c == '"' || c == '\'')
        {
            inQuote = true;
            quote = c;
        }
        else if (c == '#')
            return line.substr(0, i);
    }
    return line;
}

std::vector<std::string> ParseList(const std::string& v)
{
    std::vector<std::string> items;
    std::string body = Trim(v);
    if (body.empty())
        return items;
    if (body.front() != '[')
    {
        items.push_back(Unquote(body));
        return items;
    }
    body = body.substr(1, body.size() >= 2 ? body.size() - 2 : 0);
    size_t start = 0;
    while (start <= body.size())
    {
        size_t comma = body.find(',', start);
        std::string item = Trim(body.substr(start, comma == std::string::npos ? std::string::npos : comma - start));
        if (!item.empty())
            items.push_back(Unquote(item));
        if (comma == std::string::npos)
            break;
        start = comma + 1;
    }
    return items;
}

bool ParseBool(const std::string& v, bool fallback)
{
    std::string s = Lower(Unquote(Trim(v)));
    if (s == "true" || s == "1" || s == "yes")
        return true;
    if (s == "false" || s == "0" || s == "no")
        return false;
    return fallback;
}

uint32_t ParseColor(const std::string& v, uint32_t fallback)
{
    std::string s = Unquote(Trim(v));
    if (!s.empty() && s[0] == '#')
        s = s.substr(1);
    if (s.size() > 2 && (s[1] == 'x' || s[1] == 'X'))
        s = s.substr(2);
    char* end = nullptr;
    unsigned long c = strtoul(s.c_str(), &end, 16);
    return (end && *end == 0 && !s.empty()) ? static_cast<uint32_t>(c & 0xFFFFFF) : fallback;
}

struct ActionName
{
    const char* name;
    uint32_t id;
};

const ActionName kActions[] = {
    {"test", mmio::Test}, {"service", mmio::Service}, {"start", mmio::Start},
    {"dpadup", mmio::DpadUp}, {"dpaddown", mmio::DpadDown},
    {"dpadleft", mmio::DpadLeft}, {"dpadright", mmio::DpadRight},
    {"square", mmio::Square}, {"triangle", mmio::Triangle},
    {"circle", mmio::Circle}, {"cross", mmio::Cross},
    {"l1", mmio::L1}, {"r1", mmio::R1}, {"l2", mmio::L2}, {"r2", mmio::R2},
    {"select", mmio::Select}, {"l3", mmio::L3}, {"r3", mmio::R3},
    {"sw1", mmio::Sw1}, {"sw2", mmio::Sw2}, {"pause", mmio::Pause},
};

bool LookupAction(const std::string& name, uint32_t& id)
{
    std::string n = Lower(name);
    for (const ActionName& a : kActions)
    {
        if (n == a.name)
        {
            id = a.id;
            return true;
        }
    }
    char* end = nullptr;
    unsigned long v = strtoul(n.c_str(), &end, 10);
    if (end && *end == 0 && !n.empty() && mmio::IsGameButtonValid(static_cast<uint32_t>(v)))
    {
        id = static_cast<uint32_t>(v);
        return true;
    }
    return false;
}

std::string Narrow(const std::wstring& w)
{
    if (w.empty())
        return {};
    int n = WideCharToMultiByte(CP_UTF8, 0, w.c_str(), -1, nullptr, 0, nullptr, nullptr);
    std::string s(n > 0 ? n - 1 : 0, '\0');
    if (n > 0)
        WideCharToMultiByte(CP_UTF8, 0, w.c_str(), -1, s.data(), n, nullptr, nullptr);
    return s;
}

std::wstring Widen(const std::string& s)
{
    int n = MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, nullptr, 0);
    std::wstring w(n > 0 ? n - 1 : 0, L'\0');
    if (n > 0)
        MultiByteToWideChar(CP_UTF8, 0, s.c_str(), -1, w.data(), n);
    return w;
}

void LoadConfig()
{
    std::wstring path = g_modDir + L"config.toml";
    FILE* f = _wfopen(path.c_str(), L"rb");
    if (!f)
    {
        Log("config.toml not found, using defaults");
        return;
    }
    std::string text;
    char buf[4096];
    size_t n;
    while ((n = fread(buf, 1, sizeof(buf), f)) > 0)
        text.append(buf, n);
    fclose(f);

    size_t pos = 0;
    while (pos < text.size())
    {
        size_t eol = text.find('\n', pos);
        std::string line = Trim(StripComment(text.substr(pos, eol == std::string::npos ? std::string::npos : eol - pos)));
        pos = eol == std::string::npos ? text.size() : eol + 1;
        size_t eq = line.find('=');
        if (line.empty() || line[0] == '[' || eq == std::string::npos)
            continue;
        std::string key = Lower(Trim(line.substr(0, eq)));
        std::string val = Trim(line.substr(eq + 1));

        if (key == "host")
            g_config.host = Unquote(val);
        else if (key == "port")
            g_config.port = atoi(Unquote(val).c_str());
        else if (key == "poll_interval_ms")
            g_config.poll_interval_ms = atoi(Unquote(val).c_str());
        else if (key == "slider_reverse")
            g_config.slider_reverse = ParseBool(val, g_config.slider_reverse);
        else if (key == "shared_memory")
            g_config.shared_memory = Widen(Unquote(val));
        else if (key == "leds")
            g_config.leds = Lower(Unquote(val));
        else if (key == "led_touch_color")
            g_config.led_touch_color = ParseColor(val, g_config.led_touch_color);
        else if (key == "led_idle_color")
            g_config.led_idle_color = ParseColor(val, g_config.led_idle_color);
        else if (key == "button_leds")
            g_config.button_leds = ParseBool(val, g_config.button_leds);
        else if (key == "debug")
            g_config.debug = ParseBool(val, g_config.debug);
        else
        {
            for (ButtonBinding& b : g_config.buttons)
            {
                if (key != b.key)
                    continue;
                b.actions.clear();
                for (const std::string& item : ParseList(val))
                {
                    uint32_t id;
                    if (LookupAction(item, id))
                        b.actions.push_back(id);
                    else
                        Log("%s: unknown action \"%s\" ignored", b.key, item.c_str());
                }
            }
        }
    }
    if (g_config.poll_interval_ms < 1)
        g_config.poll_interval_ms = 1;
    if (g_config.port <= 0 || g_config.port > 65535)
        g_config.port = 45710;
}

// ------------------------------------------------------------------ MMIO publisher
//
// AnySlider creates the shared memory when its IO is enabled. BridgeSlider never
// creates it (so it can't pick the wrong layout); it waits for AnySlider, reads
// the header, and speaks whichever ABI AnySlider uses:
//   v1.0 (AnySlider release 2.0): seqlock snapshot + ordered button-event ring
//   v2.0 (AnySlider after Sept 2026): ring of complete snapshots

namespace v1 = anyslider::mmio_v1;

constexpr uint32_t kSourceId = 0x42534C44;   // "BSLD"
constexpr uint64_t kLeaseMs = 250;          // must be <= AnySlider io_max_input_lease_ms

class Publisher
{
public:
    enum class Result { Ok, NotFound, Incompatible };

    // Generic state: buttons as raw MM+ action bits, touch mask bit i = cell i (left to right).
    struct State
    {
        bool active = false;
        uint64_t gamebtn[3] = {};
        uint32_t touch_mask = 0;
    };

    Result Open(const std::wstring& name)
    {
        Close();
        mapping_ = OpenFileMappingW(FILE_MAP_ALL_ACCESS, FALSE, name.c_str());
        if (!mapping_)
            return Result::NotFound;
        base_ = static_cast<uint8_t*>(MapViewOfFile(mapping_, FILE_MAP_ALL_ACCESS, 0, 0, 0));
        if (!base_)
        {
            Close();
            return Result::NotFound;
        }
        MEMORY_BASIC_INFORMATION mbi{};
        VirtualQuery(base_, &mbi, sizeof(mbi));
        const size_t viewSize = mbi.RegionSize;
        const uint32_t magic = *reinterpret_cast<uint32_t*>(base_);
        const uint16_t major = *reinterpret_cast<uint16_t*>(base_ + 4);
        const uint16_t minor = *reinterpret_cast<uint16_t*>(base_ + 6);
        const uint32_t size = *reinterpret_cast<uint32_t*>(base_ + 8);
        found_major_ = major;
        found_minor_ = minor;
        found_size_ = size;
        found_magic_ = magic;

        if (magic == mmio::kMagic && major == mmio::kAbiMajor && minor == mmio::kAbiMinor &&
            size == sizeof(mmio::SharedBuffer) && viewSize >= sizeof(mmio::SharedBuffer))
        {
            abi_ = 2;
            auto* b = reinterpret_cast<mmio::SharedBuffer*>(base_);
            auto& ep = b->producer;
            ep.process_id = GetCurrentProcessId();
            ep.protocol_major = mmio::kAbiMajor;
            ep.protocol_minor = mmio::kAbiMinor;
            ep.started_ms = GetTickCount64();
            InterlockedExchange64(reinterpret_cast<volatile LONG64*>(&ep.heartbeat_ms), static_cast<LONG64>(ep.started_ms));
            InterlockedExchange64(&b->input_sequence, 0);
            for (auto& slot : b->inputs)
                InterlockedExchange64(&slot.sequence, 0);
            return Result::Ok;
        }
        if (magic == v1::kMagic && major == v1::kAbiMajor && minor == v1::kAbiMinor &&
            size == sizeof(v1::SharedBuffer) && viewSize >= sizeof(v1::SharedBuffer))
        {
            abi_ = 1;
            auto* b = reinterpret_cast<v1::SharedBuffer*>(base_);
            auto& ep = b->producer;
            ep.process_id = GetCurrentProcessId();
            ep.protocol_major = v1::kAbiMajor;
            ep.protocol_minor = v1::kAbiMinor;
            ep.started_us = GetTickCount64() * 1000;
            ep.heartbeat_us = ep.started_us;
            std::memset(v1_last_, 0, sizeof(v1_last_));
            v1_published_ = false;
            return Result::Ok;
        }
        Close();
        return Result::Incompatible;
    }

    void Close()
    {
        if (base_)
            UnmapViewOfFile(base_);
        if (mapping_)
            CloseHandle(mapping_);
        base_ = nullptr;
        mapping_ = nullptr;
        abi_ = 0;
    }

    bool IsOpen() const { return abi_ != 0; }
    int Abi() const { return abi_; }
    void Found(uint32_t& magic, uint16_t& major, uint16_t& minor, uint32_t& size) const
    {
        magic = found_magic_; major = found_major_; minor = found_minor_; size = found_size_;
    }

    void Publish(const State& st)
    {
        if (abi_ == 2)
            PublishV2(st);
        else if (abi_ == 1)
            PublishV1(st);
    }

    uint64_t HookHeartbeat() const
    {
        if (abi_ == 2)
            return reinterpret_cast<const mmio::SharedBuffer*>(base_)->hook.heartbeat_ms;
        if (abi_ == 1)
            return reinterpret_cast<const v1::SharedBuffer*>(base_)->hook.heartbeat_us;
        return 0;
    }

private:
    void PublishV2(const State& st)
    {
        auto* b = reinterpret_cast<mmio::SharedBuffer*>(base_);
        mmio::InputSnapshot snap{};
        snap.mode = static_cast<uint32_t>(st.active ? mmio::Mode::ArcadeSlider : mmio::Mode::None);
        if (st.active)
        {
            std::memcpy(snap.gamebtn, st.gamebtn, sizeof(snap.gamebtn));
            snap.touch_mask = st.touch_mask;
        }
        snap.timestamp_ms = GetTickCount64();
        const int64_t next = InterlockedCompareExchange64(&b->input_sequence, 0, 0) + 1;
        auto& slot = b->inputs[mmio::InputSlotIndex(next)];
        InterlockedExchange64(&slot.sequence, 0);
        MemoryBarrier();
        std::memcpy(&slot.input, &snap, sizeof(snap));
        MemoryBarrier();
        InterlockedExchange64(&slot.sequence, next);
        MemoryBarrier();
        InterlockedExchange64(&b->input_sequence, next);
        InterlockedExchange64(reinterpret_cast<volatile LONG64*>(&b->producer.heartbeat_ms),
                              static_cast<LONG64>(GetTickCount64()));
    }

    void PublishV1(const State& st)
    {
        auto* b = reinterpret_cast<v1::SharedBuffer*>(base_);
        v1::InputSnapshot snap{};
        snap.mode = static_cast<uint32_t>(st.active ? v1::Mode::ArcadeSlider : v1::Mode::None);
        if (st.active)
        {
            std::memcpy(snap.gamebtn, st.gamebtn, sizeof(snap.gamebtn));
            for (uint32_t i = 0; i < v1::kTouchCellCount; ++i)
                snap.touch_cells[i] = static_cast<uint8_t>((st.touch_mask >> i) & 1u);
        }
        snap.source_id = kSourceId;
        const uint64_t nowUs = GetTickCount64() * 1000;
        snap.timestamp_us = nowUs;
        snap.lease_ms = kLeaseMs;

        const int64_t cur = InterlockedCompareExchange64(&b->input_sequence, 0, 0);
        const int64_t even = cur & ~int64_t{1};   // we are the only producer
        uint64_t pressed[3], released[3];
        bool changed = false;
        for (int w = 0; w < 3; ++w)
        {
            const uint64_t prev = v1_published_ ? v1_last_[w] : 0;
            pressed[w] = snap.gamebtn[w] & ~prev;
            released[w] = prev & ~snap.gamebtn[w];
            changed = changed || pressed[w] || released[w];
        }
        InterlockedExchange64(&b->input_sequence, even + 1);   // odd: writing
        MemoryBarrier();
        std::memcpy(&b->input, &snap, sizeof(snap));
        if (changed)
        {
            const int64_t nextEvent = InterlockedCompareExchange64(&b->button_event_sequence, 0, 0) + 1;
            auto& ev = b->button_events[static_cast<uint64_t>(nextEvent) % v1::kButtonEventCapacity];
            InterlockedExchange64(&ev.sequence, 0);
            MemoryBarrier();
            ev.source_id = snap.source_id;
            ev.mode = snap.mode;
            std::memcpy(ev.gamebtn_pressed, pressed, sizeof(pressed));
            std::memcpy(ev.gamebtn_released, released, sizeof(released));
            ev.producer_started_us = b->producer.started_us;
            ev.timestamp_us = nowUs;
            MemoryBarrier();
            InterlockedExchange64(&ev.sequence, nextEvent);
            MemoryBarrier();
            InterlockedExchange64(&b->button_event_sequence, nextEvent);
        }
        b->producer.heartbeat_us = nowUs;
        MemoryBarrier();
        InterlockedExchange64(&b->input_sequence, even + 2);   // even: stable
        std::memcpy(v1_last_, snap.gamebtn, sizeof(v1_last_));
        v1_published_ = true;
    }

    HANDLE mapping_ = nullptr;
    uint8_t* base_ = nullptr;
    int abi_ = 0;
    uint64_t v1_last_[3] = {};
    bool v1_published_ = false;
    uint32_t found_magic_ = 0, found_size_ = 0;
    uint16_t found_major_ = 0, found_minor_ = 0;
};

// ------------------------------------------------------------------ bridge client

class BridgeClient
{
public:
    ~BridgeClient() { Close(); }

    bool Connect(const Config& cfg)
    {
        Close();
        addrinfo hints{};
        hints.ai_family = AF_UNSPEC;
        hints.ai_socktype = SOCK_STREAM;
        hints.ai_protocol = IPPROTO_TCP;
        addrinfo* res = nullptr;
        char port[16];
        snprintf(port, sizeof(port), "%d", cfg.port);
        if (getaddrinfo(cfg.host.c_str(), port, &hints, &res) != 0 || !res)
            return false;
        for (addrinfo* ai = res; ai; ai = ai->ai_next)
        {
            SOCKET s = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
            if (s == INVALID_SOCKET)
                continue;
            if (connect(s, ai->ai_addr, static_cast<int>(ai->ai_addrlen)) == 0)
            {
                sock_ = s;
                break;
            }
            closesocket(s);
        }
        freeaddrinfo(res);
        if (sock_ == INVALID_SOCKET)
            return false;
        BOOL nodelay = TRUE;
        setsockopt(sock_, IPPROTO_TCP, TCP_NODELAY, reinterpret_cast<const char*>(&nodelay), sizeof(nodelay));
        DWORD timeout = 1000;
        setsockopt(sock_, SOL_SOCKET, SO_RCVTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout));
        setsockopt(sock_, SOL_SOCKET, SO_SNDTIMEO, reinterpret_cast<const char*>(&timeout), sizeof(timeout));
        return true;
    }

    void Close()
    {
        if (sock_ != INVALID_SOCKET)
        {
            closesocket(sock_);
            sock_ = INVALID_SOCKET;
        }
    }

    bool Connected() const { return sock_ != INVALID_SOCKET; }

    // 'R' ep max -> status len data. Returns false on socket failure;
    // present=false when the bridge has no device.
    bool Read(char op, uint8_t maxLen, uint8_t* out, uint8_t& len, bool& present)
    {
        const uint8_t req[3] = {static_cast<uint8_t>(op), 0x84, maxLen};
        uint8_t hdr[2];
        if (!SendAll(req, sizeof(req)) || !RecvAll(hdr, 2))
            return false;
        present = hdr[0] != 0;
        len = hdr[1];
        return len == 0 || RecvAll(out, len);
    }

    // 'W' ep len data -> status
    bool Write(const uint8_t* data, uint8_t len)
    {
        uint8_t req[3 + 255];
        req[0] = 'W';
        req[1] = 0x03;
        req[2] = len;
        std::memcpy(req + 3, data, len);
        uint8_t status;
        return SendAll(req, 3 + len) && RecvAll(&status, 1);
    }

private:
    bool SendAll(const uint8_t* p, int n)
    {
        while (n > 0)
        {
            int r = send(sock_, reinterpret_cast<const char*>(p), n, 0);
            if (r <= 0)
                return false;
            p += r;
            n -= r;
        }
        return true;
    }

    bool RecvAll(uint8_t* p, int n)
    {
        while (n > 0)
        {
            int r = recv(sock_, reinterpret_cast<char*>(p), n, 0);
            if (r <= 0)
                return false;
            p += r;
            n -= r;
        }
        return true;
    }

    SOCKET sock_ = INVALID_SOCKET;
};

// ------------------------------------------------------------------ translation

uint32_t Reverse32(uint32_t v)
{
    uint32_t r = 0;
    for (int i = 0; i < 32; ++i)
        r |= ((v >> i) & 1u) << (31 - i);
    return r;
}

// Divaller packet -> MMIO snapshot. Slider cell i (0 = left) is TLAC sensor i,
// which is bit i of (s5>>4 | s6<<4 | s7<<12 | s8<<20 | s9<<28).
Publisher::State Translate(const uint8_t* pkt, uint8_t len, const Config& cfg)
{
    uint8_t s[26] = {};   // 24-byte Divaller packet + 2 bytes of extended buttons
    std::memcpy(s, pkt, len < sizeof(s) ? len : sizeof(s));
    Publisher::State snap{};
    snap.active = true;
    if (s[0] != 0x42 || s[1] != 0x56 || s[2] != 0x5A)
        return snap;   // no valid packet: nothing pressed
    uint32_t mask = (static_cast<uint32_t>(s[5]) >> 4) | (static_cast<uint32_t>(s[6]) << 4) |
                    (static_cast<uint32_t>(s[7]) << 12) | (static_cast<uint32_t>(s[8]) << 20) |
                    (static_cast<uint32_t>(s[9] & 0x0F) << 28);
    snap.touch_mask = cfg.slider_reverse ? Reverse32(mask) : mask;
    for (const ButtonBinding& b : cfg.buttons)
    {
        if ((s[b.byte] >> b.bit) & 1)
        {
            for (uint32_t id : b.actions)
                mmio::SetGameButton(snap.gamebtn, id);
        }
    }
    return snap;
}

// MMIO touch mask -> 100-byte Divaller LED packet ("DLa"), slider lit where touched.
// TLAC layout: sensor n's colour lives at 4 + 3*(31-n) as G, R, B.
void BuildLedPacket(uint32_t cellMask, const Config& cfg, uint8_t (&out)[100])
{
    std::memset(out, 0, sizeof(out));
    out[0] = 0x44;
    out[1] = 0x4C;
    out[2] = 0x61;
    // Button LEDs: bits 4-7 of byte 3, written inverted by TLAC (0 = lit).
    out[3] = cfg.button_leds ? 0x00 : 0xF0;
    for (int n = 0; n < 32; ++n)
    {
        // cellMask is in game order; undo slider_reverse to get the device sensor index.
        const int cell = cfg.slider_reverse ? 31 - n : n;
        const uint32_t c = ((cellMask >> cell) & 1) ? cfg.led_touch_color : cfg.led_idle_color;
        const int o = 4 + 3 * (31 - n);
        out[o] = static_cast<uint8_t>(c >> 8);       // G
        out[o + 1] = static_cast<uint8_t>(c >> 16);  // R
        out[o + 2] = static_cast<uint8_t>(c);        // B
    }
}

// ------------------------------------------------------------------ worker

DWORD WINAPI Worker(LPVOID)
{
    timeBeginPeriod(1);
    WSADATA wsa;
    if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0)
    {
        Log("WSAStartup failed");
        return 0;
    }
    Publisher pub;
    // Wait for AnySlider to create its shared memory, then match its ABI.
    const uint64_t waitStart = GetTickCount64();
    bool warnedMissing = false;
    bool warnedIncompatible = false;
    while (g_running.load())
    {
        const Publisher::Result r = pub.Open(g_config.shared_memory);
        if (r == Publisher::Result::Ok)
            break;
        if (r == Publisher::Result::Incompatible && !warnedIncompatible)
        {
            uint32_t magic, size;
            uint16_t major, minor;
            pub.Found(magic, major, minor, size);
            Log("AnySlider shared memory uses an unsupported ABI (magic %08x, v%u.%u, size %u); "
                "BridgeSlider supports v1.0 and v2.0", magic, major, minor, size);
            warnedIncompatible = true;
        }
        if (r == Publisher::Result::NotFound && !warnedMissing && GetTickCount64() - waitStart > 10000)
        {
            Log("still waiting for AnySlider's shared memory - is AnySlider installed with io_enabled = true?");
            warnedMissing = true;
        }
        Sleep(500);
    }
    if (!pub.IsOpen())
        return 0;
    Log("using AnySlider shared memory ABI v%d.0 at %s; bridge %s:%d", pub.Abi(),
        Narrow(g_config.shared_memory).c_str(), g_config.host.c_str(), g_config.port);

    BridgeClient client;
    const bool ledsOn = g_config.leds != "off";
    Publisher::State last{};
    bool havePublished = false;
    bool wasPresent = false;
    uint64_t lastPublishMs = 0;
    uint64_t lastLedMs = 0;
    uint32_t lastLedMask = 0xFFFFFFFF;
    uint64_t nextConnectMs = 0;
    bool loggedHook = false;
    // 'X' (packet + extended buttons) needs diva_bridge.py. Older bridges drop the
    // connection on an unknown opcode; then we reconnect and use 'R' instead.
    bool legacyBridge = false;
    bool extConfirmed = false;

    auto publishNone = [&]() {
        Publisher::State none{};
        pub.Publish(none);
        last = none;
        havePublished = true;
        lastPublishMs = GetTickCount64();
    };

    while (g_running.load())
    {
        const uint64_t now = GetTickCount64();
        if (!loggedHook && pub.HookHeartbeat() != 0)
        {
            Log("AnySlider IO is reading the shared memory");
            loggedHook = true;
        }

        if (!client.Connected())
        {
            if (now < nextConnectMs)
            {
                Sleep(50);
                continue;
            }
            if (!client.Connect(g_config))
            {
                legacyBridge = false;       // nothing listening: probe 'X' again next time
                if (havePublished && last.active)
                    publishNone();
                nextConnectMs = now + 1000;
                continue;
            }
            Log(legacyBridge ? "connected to bridge (basic protocol)" : "connected to bridge");
            lastLedMask = 0xFFFFFFFF;
            extConfirmed = false;
        }

        uint8_t pkt[255];
        uint8_t len = 0;
        bool present = false;
        if (!client.Read(legacyBridge ? 'R' : 'X', legacyBridge ? 24 : 26, pkt, len, present))
        {
            client.Close();
            if (!legacyBridge && !extConfirmed)
            {
                Log("bridge doesn't support extended buttons (older divaller/zhousensor bridge); "
                    "using basic protocol - D-pad and shoulder buttons unavailable");
                legacyBridge = true;
                nextConnectMs = 0;          // reconnect straight away
                continue;
            }
            Log("bridge connection lost");
            legacyBridge = false;           // probe again after the bridge restarts
            publishNone();
            nextConnectMs = now + 1000;
            continue;
        }
        extConfirmed = true;

        if (present != wasPresent)
        {
            Log(present ? "controller present" : "bridge reports no controller");
            wasPresent = present;
        }

        if (!present)
        {
            if (!havePublished || last.active || now - lastPublishMs > 100)
                publishNone();
            Sleep(20);
            continue;
        }

        Publisher::State snap = Translate(pkt, len, g_config);
        const bool changed = !havePublished || snap.active != last.active || snap.touch_mask != last.touch_mask ||
                             std::memcmp(snap.gamebtn, last.gamebtn, sizeof(snap.gamebtn)) != 0;
        // Publish every change, and refresh well inside AnySlider's input lease.
        if (changed || now - lastPublishMs >= 50)
        {
            pub.Publish(snap);
            if (g_config.debug && changed)
                Log("mask %08x buttons %016llx", snap.touch_mask,
                    static_cast<unsigned long long>(snap.gamebtn[0]));
            last = snap;
            havePublished = true;
            lastPublishMs = now;
        }

        if (ledsOn && (snap.touch_mask != lastLedMask || now - lastLedMs >= 250))
        {
            uint8_t led[100];
            BuildLedPacket(snap.touch_mask, g_config, led);
            if (!client.Write(led, sizeof(led)))
            {
                Log("bridge connection lost (LED write)");
                client.Close();
                publishNone();
                nextConnectMs = now + 1000;
                continue;
            }
            lastLedMask = snap.touch_mask;
            lastLedMs = now;
        }

        Sleep(static_cast<DWORD>(g_config.poll_interval_ms));
    }
    client.Close();
    publishNone();
    WSACleanup();
    timeEndPeriod(1);
    return 0;
}

void FindModDir(HMODULE self)
{
    wchar_t path[MAX_PATH];
    DWORD n = GetModuleFileNameW(self, path, MAX_PATH);
    std::wstring p(path, n);
    size_t slash = p.find_last_of(L"\\/");
    g_modDir = slash == std::wstring::npos ? L"" : p.substr(0, slash + 1);
}

HMODULE g_self = nullptr;
}   // namespace

extern "C"
{
BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID)
{
    if (reason == DLL_PROCESS_ATTACH)
    {
        g_self = inst;
        DisableThreadLibraryCalls(inst);
    }
    return TRUE;
}

// DIVA Mod Loader calls this once the game has started.
__declspec(dllexport) void Init()
{
    if (g_running.exchange(true))
        return;
    FindModDir(g_self);
    g_log = _wfopen((g_modDir + L"BridgeSlider.log").c_str(), L"w");
    LoadConfig();
    Log("BridgeSlider 1.2 starting");
    HANDLE t = CreateThread(nullptr, 0, Worker, nullptr, 0, nullptr);
    if (t)
    {
        SetThreadPriority(t, THREAD_PRIORITY_ABOVE_NORMAL);
        CloseHandle(t);
    }
    else
    {
        Log("could not start worker thread");
        g_running = false;
    }
}
}
