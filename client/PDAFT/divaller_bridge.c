/*
 * DivallerBridge.dva — Wine shim that restores TLAC's Divaller support.
 *
 * Why this exists
 * ---------------
 * TLAC finds the Divaller (VID 0E8F / PID 2213) with SetupAPI, opens it with
 * CreateFileW and talks to it through WinUSB (read pipe 0x84, write pipe 0x03).
 * Under Wine none of that works: wineusb.sys does not publish
 * GUID_DEVINTERFACE_USB_DEVICE interfaces, so enumeration finds nothing, and
 * Wine's winusb.dll is nothing but stubs.
 *
 * What it does
 * ------------
 * PD-Loader loads every *.dva in its plugins folder. This one waits for
 * TLAC.dva to be loaded, then rewrites TLAC's import table so that its SetupAPI,
 * CreateFileW and WinUsb_* calls land here. A fake device is reported, and the
 * pipe reads/writes are forwarded over TCP to divaller_bridge.py (by default
 * on 127.0.0.1), which owns the real device on the Linux side through libusb.
 *
 * Nothing outside TLAC.dva is touched, and any call that is not about the
 * Divaller is passed through to the real function.
 *
 * Settings live in DivallerBridge.ini next to the plugin ([bridge] host=,
 * port=) and show up in the PD-Loader launcher. Environment variables (Wine
 * passes Unix ones through) override them:
 *   DIVALLER_BRIDGE_HOST    address of the bridge (default 127.0.0.1)
 *   DIVALLER_BRIDGE_PORT    TCP port of the bridge (default 45710)
 *   DIVALLER_BRIDGE_ALWAYS  1/0: keep TLAC attached while the bridge is away
 *   DIVALLER_BRIDGE_BACKGROUND 1/0: let TLAC read input while unfocused
 *   DIVALLER_BRIDGE_TARGET  module to patch (default TLAC.dva)
 *
 * Build (no CRT needed):
 *   x86_64-w64-mingw32-gcc -O2 -shared -nostdlib -mno-stack-arg-probe \
 *     -o DivallerBridge.dva divaller_bridge.c -Wl,-e,DllMain \
 *     -lkernel32 -lws2_32 -luser32
 */

#define WIN32_LEAN_AND_MEAN
#define UNICODE
#define _UNICODE
#include <winsock2.h>
#include <ws2tcpip.h>
#include <windows.h>
#include <setupapi.h>
#include <winusb.h>
#include <dbt.h>

/* ---------------------------------------------------------------- basics */

/* Built without a CRT, so provide what the compiler may emit calls to. */
void *memset(void *d, int c, size_t n)
{
    unsigned char *p = (unsigned char *)d;
    while (n--) *p++ = (unsigned char)c;
    return d;
}
void *memcpy(void *d, const void *s, size_t n)
{
    unsigned char *p = (unsigned char *)d;
    const unsigned char *q = (const unsigned char *)s;
    while (n--) *p++ = *q++;
    return d;
}

static int ieq_a(const char *a, const char *b)
{
    for (;; a++, b++) {
        char x = *a, y = *b;
        if (x >= 'A' && x <= 'Z') x += 32;
        if (y >= 'A' && y <= 'Z') y += 32;
        if (x != y) return 0;
        if (!x) return 1;
    }
}

static int str_eq(const char *a, const char *b)
{
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}

static int wcontains_i(const WCHAR *hay, const WCHAR *needle)
{
    if (!hay) return 0;
    for (; *hay; hay++) {
        const WCHAR *h = hay, *n = needle;
        for (; *h && *n; h++, n++) {
            WCHAR x = *h, y = *n;
            if (x >= 'A' && x <= 'Z') x += 32;
            if (y >= 'A' && y <= 'Z') y += 32;
            if (x != y) break;
        }
        if (!*n) return 1;
    }
    return 0;
}

static HANDLE g_logfile = INVALID_HANDLE_VALUE;

/* Logs go to stdout, the debugger, and DivallerBridge.log next to the plugin
 * (stdout usually isn't visible under Proton). */
static void blog(const char *fmt, ...)
{
    char buf[512];
    SYSTEMTIME t;
    GetLocalTime(&t);
    int n = wsprintfA(buf, "%02d:%02d:%02d [DivallerBridge] ", t.wHour, t.wMinute, t.wSecond);
    va_list ap;
    va_start(ap, fmt);
    n += wvsprintfA(buf + n, fmt, ap);
    va_end(ap);
    if (n > (int)sizeof(buf) - 2) n = sizeof(buf) - 2;
    buf[n++] = '\n';
    buf[n] = 0;
    OutputDebugStringA(buf);
    HANDLE out = GetStdHandle(STD_OUTPUT_HANDLE);
    if (out && out != INVALID_HANDLE_VALUE) {
        DWORD w;
        WriteFile(out, buf, (DWORD)n, &w, NULL);
    }
    if (g_logfile != INVALID_HANDLE_VALUE) {
        DWORD w;
        WriteFile(g_logfile, buf, (DWORD)n, &w, NULL);
    }
}

/* "...\plugins\DivallerBridge.dva" -> "...\plugins\DivallerBridge.<ext>" */
static int sibling_path(HINSTANCE self, const WCHAR *ext, WCHAR *out)
{
    DWORD n = GetModuleFileNameW(self, out, MAX_PATH);
    if (n == 0 || n >= MAX_PATH - 8) return 0;
    while (n > 0 && out[n - 1] != '.' && out[n - 1] != '\\' && out[n - 1] != '/') n--;
    if (n == 0 || out[n - 1] != '.') return 0;
    while (*ext) out[n++] = *ext++;
    out[n] = 0;
    return 1;
}

static void open_log(HINSTANCE self)
{
    WCHAR path[MAX_PATH];
    if (!sibling_path(self, L"log", path)) return;
    g_logfile = CreateFileW(path, GENERIC_WRITE, FILE_SHARE_READ | FILE_SHARE_WRITE, NULL,
                            CREATE_ALWAYS, FILE_ATTRIBUTE_NORMAL, NULL);
}

/* ------------------------------------------------------- bridge client */

#define DEFAULT_PORT  45710
#define DEFAULT_HOST  L"127.0.0.1"
#define EP_IN         0x84
#define EP_OUT        0x03
#define PKT_MAX       64
#define LED_MAX       255

/* All network I/O happens on one background thread (io_thread), so the game
 * thread never waits on a connect, a DNS lookup or a round trip. The hooks
 * TLAC calls only read and write this shared state. */
static CRITICAL_SECTION g_lock;
static int g_connected;                 /* socket to the bridge is up */
static int g_present;                   /* bridge reports a Divaller */
static unsigned char g_in[PKT_MAX];     /* newest input packet */
static int g_in_len;
static unsigned char g_led[LED_MAX];    /* newest LED packet from TLAC */
static int g_led_len;
static int g_led_pending;

/* Settings: DivallerBridge.ini next to the plugin (editable in the PD-Loader
 * launcher), overridden by DIVALLER_BRIDGE_HOST / DIVALLER_BRIDGE_PORT. */
static WCHAR g_ini_path[MAX_PATH];
static WCHAR g_host[256] = DEFAULT_HOST;
static unsigned short g_port = DEFAULT_PORT;
/* Keep a Divaller attached to TLAC even while the bridge or the controller is
 * away (it reads as "nothing pressed"), so TLAC never has to re-detect it. */
static volatile LONG g_always = 1;
/* Let TLAC read the Divaller (and other controllers) while the game window
 * isn't focused; keyboard and mouse stay muted then. */
static volatile LONG g_background = 1;

/* On/off setting: environment variable, else [bridge] key in the ini. */
static int read_flag(const WCHAR *env, const WCHAR *key, int def)
{
    WCHAR flag[16];
    if (GetEnvironmentVariableW(env, flag, 16) - 1u >= 15u) {
        if (g_ini_path[0])
            GetPrivateProfileStringW(L"bridge", key, def ? L"1" : L"0", flag, 16, g_ini_path);
        else
            flag[0] = def ? '1' : '0', flag[1] = 0;
    }
    WCHAR f0 = flag[0] == ' ' || flag[0] == '\t' ? flag[1] : flag[0];
    if (f0 == '0' || f0 == 'f' || f0 == 'F' || f0 == 'n' || f0 == 'N') return 0;
    if (f0 == '1' || f0 == 't' || f0 == 'T' || f0 == 'y' || f0 == 'Y') return 1;
    if (f0 == 'o' || f0 == 'O') {
        WCHAR f1 = flag[f0 == flag[0] ? 1 : 2];
        return !(f1 == 'f' || f1 == 'F');
    }
    return def;
}

static int wstr_eq(const WCHAR *a, const WCHAR *b)
{
    while (*a && *a == *b) { a++; b++; }
    return *a == *b;
}

static void wtrim(WCHAR *s)
{
    WCHAR *p = s;
    while (*p == ' ' || *p == '\t') p++;
    WCHAR *d = s;
    while (*p) *d++ = *p++;
    *d = 0;
    while (d > s && (d[-1] == ' ' || d[-1] == '\t' || d[-1] == '\r' || d[-1] == '\n')) *--d = 0;
}

/* Re-read settings. Returns 1 if host or port changed. */
static int load_settings(void)
{
    WCHAR host[256];
    unsigned port = DEFAULT_PORT;

    if (GetEnvironmentVariableW(L"DIVALLER_BRIDGE_HOST", host, 256) - 1u >= 255u) {
        if (g_ini_path[0])
            GetPrivateProfileStringW(L"bridge", L"host", DEFAULT_HOST, host, 256, g_ini_path);
        else
            memcpy(host, DEFAULT_HOST, sizeof(DEFAULT_HOST));
    }
    wtrim(host);
    if (!host[0]) memcpy(host, DEFAULT_HOST, sizeof(DEFAULT_HOST));

    char buf[16];
    DWORD n = GetEnvironmentVariableA("DIVALLER_BRIDGE_PORT", buf, sizeof(buf));
    if (n > 0 && n < sizeof(buf)) {
        unsigned v = 0;
        for (DWORD i = 0; i < n && buf[i] >= '0' && buf[i] <= '9'; i++) v = v * 10 + (buf[i] - '0');
        port = v;
    } else if (g_ini_path[0]) {
        port = GetPrivateProfileIntW(L"bridge", L"port", DEFAULT_PORT, g_ini_path);
    }
    if (port == 0 || port > 65535) port = DEFAULT_PORT;

    int always = read_flag(L"DIVALLER_BRIDGE_ALWAYS", L"always_connected", 1);
    if (always != g_always) {
        InterlockedExchange(&g_always, always);
        blog(always ? "always-connected mode on" : "always-connected mode off");
    }
    int background = read_flag(L"DIVALLER_BRIDGE_BACKGROUND", L"background_input", 1);
    if (background != g_background) {
        InterlockedExchange(&g_background, background);
        blog(background ? "background input on: the Divaller keeps working while the game is unfocused"
                        : "background input off");
    }

    int changed = !wstr_eq(host, g_host) || port != g_port;
    memcpy(g_host, host, sizeof(host));
    g_port = (unsigned short)port;
    return changed;
}

static int send_all(SOCKET s, const void *buf, int len)
{
    const char *p = (const char *)buf;
    while (len > 0) {
        int r = send(s, p, len, 0);
        if (r <= 0) return 0;
        p += r; len -= r;
    }
    return 1;
}

static int recv_all(SOCKET s, void *buf, int len)
{
    char *p = (char *)buf;
    while (len > 0) {
        int r = recv(s, p, len, 0);
        if (r <= 0) return 0;
        p += r; len -= r;
    }
    return 1;
}

/* Resolve g_host (an IP or a host name) and connect with a timeout. */
static SOCKET bridge_connect(void)
{
    static int wsa_ok;
    if (!wsa_ok) {
        WSADATA wsa;
        if (WSAStartup(MAKEWORD(2, 2), &wsa) != 0) return INVALID_SOCKET;
        wsa_ok = 1;
    }

    char host[256];
    if (!WideCharToMultiByte(CP_UTF8, 0, g_host, -1, host, sizeof(host), NULL, NULL)) return INVALID_SOCKET;
    char port[8];
    wsprintfA(port, "%u", (unsigned)g_port);

    struct addrinfo hints, *res = NULL;
    memset(&hints, 0, sizeof(hints));
    hints.ai_family = AF_UNSPEC;
    hints.ai_socktype = SOCK_STREAM;
    hints.ai_protocol = IPPROTO_TCP;
    if (getaddrinfo(host, port, &hints, &res) != 0 || !res) return INVALID_SOCKET;

    SOCKET s = INVALID_SOCKET;
    for (struct addrinfo *ai = res; ai && s == INVALID_SOCKET; ai = ai->ai_next) {
        s = socket(ai->ai_family, ai->ai_socktype, ai->ai_protocol);
        if (s == INVALID_SOCKET) continue;

        u_long nb = 1;
        ioctlsocket(s, FIONBIO, &nb);
        int ok = connect(s, ai->ai_addr, (int)ai->ai_addrlen) == 0;
        if (!ok && WSAGetLastError() == WSAEWOULDBLOCK) {
            fd_set w, e;
            FD_ZERO(&w); FD_SET(s, &w);
            FD_ZERO(&e); FD_SET(s, &e);
            struct timeval tv = { 2, 0 };
            if (select(0, NULL, &w, &e, &tv) == 1 && FD_ISSET(s, &w)) {
                int err = 0, len = sizeof(err);
                getsockopt(s, SOL_SOCKET, SO_ERROR, (char *)&err, &len);
                ok = err == 0;
            }
        }
        nb = 0;
        ioctlsocket(s, FIONBIO, &nb);
        if (!ok) {
            closesocket(s);
            s = INVALID_SOCKET;
        }
    }
    freeaddrinfo(res);
    if (s == INVALID_SOCKET) return s;

    BOOL nodelay = TRUE;
    setsockopt(s, IPPROTO_TCP, TCP_NODELAY, (const char *)&nodelay, sizeof(nodelay));
    DWORD timeout = 1000;
    setsockopt(s, SOL_SOCKET, SO_RCVTIMEO, (const char *)&timeout, sizeof(timeout));
    setsockopt(s, SOL_SOCKET, SO_SNDTIMEO, (const char *)&timeout, sizeof(timeout));
    return s;
}

/* Protocol (all requests start with one opcode byte):
 *   'P'                         -> status(1)                   device present?
 *   'R' ep(1) max(1)            -> status(1) len(1) data[len]  latest IN packet
 *   'W' ep(1) len(1) data[len]  -> status(1)                   queue OUT packet
 * One poll = an 'R', plus a 'W' if TLAC has sent new LED data since. */
static int bridge_poll(SOCKET s)
{
    unsigned char req[3] = { 'R', EP_IN, PKT_MAX };
    unsigned char hdr[2], data[256];
    if (!send_all(s, req, 3) || !recv_all(s, hdr, 2) || (hdr[1] && !recv_all(s, data, hdr[1])))
        return 0;

    unsigned char led[LED_MAX];
    int led_len = 0;
    EnterCriticalSection(&g_lock);
    g_present = hdr[0] == 1;
    if (g_present) {
        g_in_len = hdr[1] < PKT_MAX ? hdr[1] : PKT_MAX;
        memcpy(g_in, data, g_in_len);
    } else {
        g_in_len = 0;  /* never leave buttons "held" from the last packet */
    }
    if (g_led_pending) {
        led_len = g_led_len;
        memcpy(led, g_led, led_len);
        g_led_pending = 0;
    }
    LeaveCriticalSection(&g_lock);

    if (led_len) {
        unsigned char wreq[3] = { 'W', EP_OUT, (unsigned char)led_len };
        unsigned char st;
        if (!send_all(s, wreq, 3) || !send_all(s, led, led_len) || !recv_all(s, &st, 1))
            return 0;
    }
    return 1;
}

static void set_disconnected(void)
{
    EnterCriticalSection(&g_lock);
    g_connected = 0;
    g_present = 0;
    g_in_len = 0;
    g_led_pending = 0;
    LeaveCriticalSection(&g_lock);
}

/* ------------------------------------------------------- fake device */

/* {A5DCBF10-6530-11D2-901F-00C04FB951ED} */
static const GUID kUsbDeviceGuid =
    { 0xA5DCBF10, 0x6530, 0x11D2, { 0x90, 0x1F, 0x00, 0xC0, 0x4F, 0xB9, 0x51, 0xED } };

static const WCHAR kFakePath[] =
    L"\\\\?\\usb#vid_0e8f&pid_2213#divallerbridge#{a5dcbf10-6530-11d2-901f-00c04fb951ed}";

static int g_devinfo_tag;   /* its address is the fake HDEVINFO */
static int g_iface_tag;     /* its address is the fake WINUSB_INTERFACE_HANDLE */
static HANDLE g_fake_file;  /* a real (event) handle so TLAC's CloseHandle works */
static volatile LONG g_handed;  /* TLAC currently holds the Divaller */
static LONG g_enum_logs;

#define FAKE_DEVINFO ((HDEVINFO)&g_devinfo_tag)
#define FAKE_IFACE   ((WINUSB_INTERFACE_HANDLE)&g_iface_tag)

static int guid_eq(const GUID *a, const GUID *b)
{
    const unsigned char *x = (const unsigned char *)a, *y = (const unsigned char *)b;
    for (int i = 0; i < (int)sizeof(GUID); i++) if (x[i] != y[i]) return 0;
    return 1;
}

/* Originals, filled in while patching. */
typedef HDEVINFO (WINAPI *pfnGetClassDevsW)(const GUID *, PCWSTR, HWND, DWORD);
typedef BOOL (WINAPI *pfnEnumDeviceInterfaces)(HDEVINFO, PSP_DEVINFO_DATA, const GUID *, DWORD, PSP_DEVICE_INTERFACE_DATA);
typedef BOOL (WINAPI *pfnGetDeviceInterfaceDetailW)(HDEVINFO, PSP_DEVICE_INTERFACE_DATA, PSP_DEVICE_INTERFACE_DETAIL_DATA_W, DWORD, PDWORD, PSP_DEVINFO_DATA);
typedef BOOL (WINAPI *pfnDestroyDeviceInfoList)(HDEVINFO);
typedef HANDLE (WINAPI *pfnCreateFileW)(LPCWSTR, DWORD, DWORD, LPSECURITY_ATTRIBUTES, DWORD, DWORD, HANDLE);
typedef BOOL (WINAPI *pfnWinUsbInitialize)(HANDLE, PWINUSB_INTERFACE_HANDLE);
typedef BOOL (WINAPI *pfnWinUsbFree)(WINUSB_INTERFACE_HANDLE);
typedef BOOL (WINAPI *pfnWinUsbQueryInterfaceSettings)(WINUSB_INTERFACE_HANDLE, UCHAR, PUSB_INTERFACE_DESCRIPTOR);
typedef BOOL (WINAPI *pfnWinUsbPipe)(WINUSB_INTERFACE_HANDLE, UCHAR, PUCHAR, ULONG, PULONG, LPOVERLAPPED);

static pfnGetClassDevsW               o_GetClassDevsW;
static pfnEnumDeviceInterfaces        o_EnumDeviceInterfaces;
static pfnGetDeviceInterfaceDetailW   o_GetDeviceInterfaceDetailW;
static pfnDestroyDeviceInfoList       o_DestroyDeviceInfoList;
static pfnCreateFileW                 o_CreateFileW;
static pfnWinUsbInitialize            o_WinUsbInitialize;
static pfnWinUsbFree                  o_WinUsbFree;
static pfnWinUsbQueryInterfaceSettings o_WinUsbQueryInterfaceSettings;
static pfnWinUsbPipe                  o_WinUsbReadPipe;
static pfnWinUsbPipe                  o_WinUsbWritePipe;

static HDEVINFO WINAPI h_GetClassDevsW(const GUID *g, PCWSTR e, HWND w, DWORD f)
{
    if (g && guid_eq(g, &kUsbDeviceGuid) && (f & DIGCF_DEVICEINTERFACE)) {
        if (InterlockedIncrement(&g_enum_logs) <= 3) blog("TLAC is looking for the Divaller");
        return FAKE_DEVINFO;
    }
    return o_GetClassDevsW(g, e, w, f);
}

static BOOL WINAPI h_EnumDeviceInterfaces(HDEVINFO h, PSP_DEVINFO_DATA d, const GUID *g, DWORD idx, PSP_DEVICE_INTERFACE_DATA out)
{
    if (h != FAKE_DEVINFO) return o_EnumDeviceInterfaces(h, d, g, idx, out);
    EnterCriticalSection(&g_lock);
    int present = g_present || g_always;
    LeaveCriticalSection(&g_lock);
    if (idx != 0 || !present) {
        SetLastError(ERROR_NO_MORE_ITEMS);
        return FALSE;
    }
    out->InterfaceClassGuid = kUsbDeviceGuid;
    out->Flags = SPINT_ACTIVE;
    out->Reserved = 0;
    return TRUE;
}

static BOOL WINAPI h_GetDeviceInterfaceDetailW(HDEVINFO h, PSP_DEVICE_INTERFACE_DATA d,
    PSP_DEVICE_INTERFACE_DETAIL_DATA_W detail, DWORD size, PDWORD required, PSP_DEVINFO_DATA info)
{
    if (h != FAKE_DEVINFO) return o_GetDeviceInterfaceDetailW(h, d, detail, size, required, info);
    DWORD need = (DWORD)(FIELD_OFFSET(SP_DEVICE_INTERFACE_DETAIL_DATA_W, DevicePath) + sizeof(kFakePath));
    if (required) *required = need;
    if (!detail || size < need) {
        SetLastError(ERROR_INSUFFICIENT_BUFFER);
        return FALSE;
    }
    memcpy(detail->DevicePath, kFakePath, sizeof(kFakePath));
    return TRUE;
}

static BOOL WINAPI h_DestroyDeviceInfoList(HDEVINFO h)
{
    if (h == FAKE_DEVINFO) return TRUE;
    return o_DestroyDeviceInfoList(h);
}

static HANDLE WINAPI h_CreateFileW(LPCWSTR name, DWORD acc, DWORD share, LPSECURITY_ATTRIBUTES sa,
    DWORD disp, DWORD flags, HANDLE tmpl)
{
    if (wcontains_i(name, L"#divallerbridge#")) {
        HANDLE ev = CreateEventW(NULL, TRUE, FALSE, NULL);
        g_fake_file = ev;
        return ev ? ev : INVALID_HANDLE_VALUE;
    }
    return o_CreateFileW(name, acc, share, sa, disp, flags, tmpl);
}

static BOOL WINAPI h_WinUsbInitialize(HANDLE dev, PWINUSB_INTERFACE_HANDLE out)
{
    if (dev != g_fake_file || !dev) return o_WinUsbInitialize(dev, out);
    EnterCriticalSection(&g_lock);
    int present = g_present || g_always;
    LeaveCriticalSection(&g_lock);
    if (!present) {
        blog("TLAC opened the Divaller but the bridge has no device");
        SetLastError(ERROR_DEVICE_NOT_CONNECTED);
        return FALSE;
    }
    *out = FAKE_IFACE;
    InterlockedExchange(&g_handed, 1);
    blog("Divaller handed to TLAC");
    return TRUE;
}

static BOOL WINAPI h_WinUsbFree(WINUSB_INTERFACE_HANDLE h)
{
    if (h == FAKE_IFACE) return TRUE;
    return o_WinUsbFree(h);
}

static BOOL WINAPI h_WinUsbQueryInterfaceSettings(WINUSB_INTERFACE_HANDLE h, UCHAR alt, PUSB_INTERFACE_DESCRIPTOR d)
{
    if (h != FAKE_IFACE) return o_WinUsbQueryInterfaceSettings(h, alt, d);
    memset(d, 0, sizeof(*d));
    d->bLength = 9;
    d->bDescriptorType = 4;      /* INTERFACE */
    d->bAlternateSetting = alt;
    d->bNumEndpoints = 2;
    d->bInterfaceClass = 0xFF;   /* vendor specific */
    return TRUE;
}

static BOOL WINAPI h_WinUsbReadPipe(WINUSB_INTERFACE_HANDLE h, UCHAR ep, PUCHAR buf, ULONG len, PULONG got, LPOVERLAPPED ov)
{
    if (h != FAKE_IFACE) return o_WinUsbReadPipe(h, ep, buf, len, got, ov);
    if (got) *got = 0;
    (void)ep;
    int ok;
    EnterCriticalSection(&g_lock);
    ok = g_present || g_always;
    if (ok) {
        /* No live data (bridge or controller away, or no packet yet):
         * report "header, nothing pressed". */
        static const unsigned char idle[24] = { 0x42, 0x56, 0x5A };
        int live = g_present && g_in_len;
        const unsigned char *src = live ? g_in : idle;
        ULONG n = live ? (ULONG)g_in_len : sizeof(idle);
        if (n > len) n = len;
        memcpy(buf, src, n);
        if (got) *got = n;
    }
    LeaveCriticalSection(&g_lock);
    if (!ok) {
        /* TLAC drops the device after a failed read. */
        if (InterlockedExchange(&g_handed, 0))
            blog("lost the Divaller; TLAC released it (it will be handed back when the bridge returns)");
        SetLastError(ERROR_GEN_FAILURE);
        return FALSE;
    }
    return TRUE;
}

static BOOL WINAPI h_WinUsbWritePipe(WINUSB_INTERFACE_HANDLE h, UCHAR ep, PUCHAR buf, ULONG len, PULONG put, LPOVERLAPPED ov)
{
    if (h != FAKE_IFACE) return o_WinUsbWritePipe(h, ep, buf, len, put, ov);
    if (put) *put = 0;
    (void)ep;
    if (len > LED_MAX) len = LED_MAX;
    int ok;
    EnterCriticalSection(&g_lock);
    ok = g_connected || g_always;
    if (g_connected) {
        /* Sent by io_thread; only the newest frame matters. */
        memcpy(g_led, buf, len);
        g_led_len = (int)len;
        g_led_pending = 1;
    }
    LeaveCriticalSection(&g_lock);
    if (!ok) {
        SetLastError(ERROR_GEN_FAILURE);
        return FALSE;
    }
    if (put) *put = len;
    return TRUE;
}

/* ------------------------------------------------------- background input */

/* TLAC reads every input device only while
 *     GetForegroundWindow() == <game window>
 * With background input on, TLAC's GetForegroundWindow always returns the game
 * window, so it keeps polling the Divaller (and XInput/DS4 pads) when another
 * window has focus. TLAC reads the keyboard and mouse buttons through
 * GetAsyncKeyState and the pointer through GetCursorPos, so those are muted
 * (no keys, frozen pointer) while the game isn't really in front; otherwise
 * typing in another window would reach the game. */
typedef HWND (WINAPI *pfnGetForegroundWindow)(void);
typedef HWND (WINAPI *pfnWindowFromDC)(HDC);
typedef SHORT (WINAPI *pfnGetAsyncKeyState)(int);
typedef BOOL (WINAPI *pfnGetCursorPos)(LPPOINT);

static pfnGetForegroundWindow o_GetForegroundWindow;
static pfnWindowFromDC        o_WindowFromDC;
static pfnGetAsyncKeyState    o_GetAsyncKeyState;
static pfnGetCursorPos        o_GetCursorPos;

static HWND g_game_wnd;          /* the game's window, as TLAC knows it */
static POINT g_last_cursor;
static int g_have_cursor;
static int g_was_background;
/* Real focus as of TLAC's per-frame GetForegroundWindow check. TLAC polls the
 * keyboard (256 GetAsyncKeyState calls) right after that check, so the cached
 * answer avoids a window-manager query per key. */
static volatile int g_real_focus = 1;

static int window_is_ours(HWND w)
{
    DWORD pid = 0;
    return w && GetWindowThreadProcessId(w, &pid) && pid == GetCurrentProcessId();
}

static int really_focused(void)
{
    return g_real_focus;
}

/* TLAC gets its window handle from WindowFromDC(wglGetCurrentDC()). */
static HWND WINAPI h_WindowFromDC(HDC dc)
{
    HWND w = o_WindowFromDC(dc);
    if (w && !g_game_wnd) {
        g_game_wnd = w;
        blog("game window found");
    }
    return w;
}

static HWND WINAPI h_GetForegroundWindow(void)
{
    HWND fg = o_GetForegroundWindow();
    int ours = window_is_ours(fg);
    g_real_focus = ours;
    if (ours) g_game_wnd = fg;  /* follow the window TLAC sees while focused */

    if (!g_background || !g_game_wnd) {
        g_was_background = 0;
        return fg;
    }
    if ((!ours) != g_was_background) {
        g_was_background = !ours;
        blog(g_was_background ? "game in background: still reading the Divaller; keyboard and mouse muted"
                              : "game focused again");
    }
    return ours ? fg : g_game_wnd;
}

static SHORT WINAPI h_GetAsyncKeyState(int vk)
{
    if (g_background && !really_focused()) return 0;
    return o_GetAsyncKeyState(vk);
}

static BOOL WINAPI h_GetCursorPos(LPPOINT pt)
{
    if (g_background && g_have_cursor && pt && !really_focused()) {
        *pt = g_last_cursor;
        return TRUE;
    }
    BOOL ok = o_GetCursorPos(pt);
    if (ok && pt) {
        g_last_cursor = *pt;
        g_have_cursor = 1;
    }
    return ok;
}

/* ------------------------------------------------------- IAT patching */

struct hook {
    const char *dll;     /* import module, case-insensitive */
    const char *name;    /* imported function name */
    void *replacement;
    void **original;
};

static struct hook g_hooks[] = {
    { "setupapi.dll", "SetupDiGetClassDevsW",             (void *)h_GetClassDevsW,              (void **)&o_GetClassDevsW },
    { "setupapi.dll", "SetupDiEnumDeviceInterfaces",      (void *)h_EnumDeviceInterfaces,       (void **)&o_EnumDeviceInterfaces },
    { "setupapi.dll", "SetupDiGetDeviceInterfaceDetailW", (void *)h_GetDeviceInterfaceDetailW,  (void **)&o_GetDeviceInterfaceDetailW },
    { "setupapi.dll", "SetupDiDestroyDeviceInfoList",     (void *)h_DestroyDeviceInfoList,      (void **)&o_DestroyDeviceInfoList },
    { "kernel32.dll", "CreateFileW",                      (void *)h_CreateFileW,                (void **)&o_CreateFileW },
    { "winusb.dll",   "WinUsb_Initialize",                (void *)h_WinUsbInitialize,           (void **)&o_WinUsbInitialize },
    { "winusb.dll",   "WinUsb_Free",                      (void *)h_WinUsbFree,                 (void **)&o_WinUsbFree },
    { "winusb.dll",   "WinUsb_QueryInterfaceSettings",    (void *)h_WinUsbQueryInterfaceSettings, (void **)&o_WinUsbQueryInterfaceSettings },
    { "winusb.dll",   "WinUsb_ReadPipe",                  (void *)h_WinUsbReadPipe,             (void **)&o_WinUsbReadPipe },
    { "winusb.dll",   "WinUsb_WritePipe",                 (void *)h_WinUsbWritePipe,            (void **)&o_WinUsbWritePipe },
    { "user32.dll",   "GetForegroundWindow",              (void *)h_GetForegroundWindow,        (void **)&o_GetForegroundWindow },
    { "user32.dll",   "WindowFromDC",                     (void *)h_WindowFromDC,               (void **)&o_WindowFromDC },
    { "user32.dll",   "GetAsyncKeyState",                 (void *)h_GetAsyncKeyState,           (void **)&o_GetAsyncKeyState },
    { "user32.dll",   "GetCursorPos",                     (void *)h_GetCursorPos,               (void **)&o_GetCursorPos },
};
#define NHOOKS (sizeof(g_hooks) / sizeof(g_hooks[0]))

static int g_has_divaller;

static int patch_module(HMODULE mod)
{
    unsigned char *base = (unsigned char *)mod;
    IMAGE_DOS_HEADER *dos = (IMAGE_DOS_HEADER *)base;
    if (dos->e_magic != IMAGE_DOS_SIGNATURE) return 0;
    IMAGE_NT_HEADERS *nt = (IMAGE_NT_HEADERS *)(base + dos->e_lfanew);
    IMAGE_DATA_DIRECTORY dir = nt->OptionalHeader.DataDirectory[IMAGE_DIRECTORY_ENTRY_IMPORT];
    if (!dir.VirtualAddress) return 0;

    int patched = 0;
    for (IMAGE_IMPORT_DESCRIPTOR *imp = (IMAGE_IMPORT_DESCRIPTOR *)(base + dir.VirtualAddress); imp->Name; imp++) {
        const char *dll = (const char *)(base + imp->Name);
        if (!imp->OriginalFirstThunk) continue;  /* need names to match on */
        IMAGE_THUNK_DATA *names = (IMAGE_THUNK_DATA *)(base + imp->OriginalFirstThunk);
        IMAGE_THUNK_DATA *iat = (IMAGE_THUNK_DATA *)(base + imp->FirstThunk);

        for (; names->u1.AddressOfData; names++, iat++) {
            if (IMAGE_SNAP_BY_ORDINAL(names->u1.Ordinal)) continue;
            const char *fn = (const char *)((IMAGE_IMPORT_BY_NAME *)(base + names->u1.AddressOfData))->Name;
            for (unsigned i = 0; i < NHOOKS; i++) {
                struct hook *h = &g_hooks[i];
                if (!ieq_a(dll, h->dll) || !str_eq(fn, h->name)) continue;
                DWORD old;
                if (!VirtualProtect(&iat->u1.Function, sizeof(void *), PAGE_READWRITE, &old)) continue;
                *h->original = (void *)iat->u1.Function;
                iat->u1.Function = (ULONG_PTR)h->replacement;
                VirtualProtect(&iat->u1.Function, sizeof(void *), old, &old);
                patched++;
                if (h->replacement == (void *)h_WinUsbReadPipe) g_has_divaller = 1;
            }
        }
    }
    return patched;
}

/* Any hook TLAC doesn't import still needs a valid original for pass-through. */
static void fill_missing_originals(void)
{
    for (unsigned i = 0; i < NHOOKS; i++) {
        struct hook *h = &g_hooks[i];
        if (*h->original) continue;
        WCHAR wdll[32];
        int j = 0;
        for (; h->dll[j] && j < 31; j++) wdll[j] = (WCHAR)h->dll[j];
        wdll[j] = 0;
        HMODULE m = LoadLibraryW(wdll);
        if (m) *h->original = (void *)GetProcAddress(m, h->name);
    }
}

static WCHAR g_target[MAX_PATH] = L"TLAC.dva";
static volatile LONG g_patched;

static int try_patch(void)
{
    if (g_patched) return 1;
    HMODULE m = GetModuleHandleW(g_target);
    if (!m) return 0;
    if (InterlockedExchange(&g_patched, 1)) return 1;
    int n = patch_module(m);
    /* fill_missing_originals() below keeps pass-through working either way. */
    fill_missing_originals();
    blog("patched %d imports in TLAC", n);
    if (!g_has_divaller) {
        blog("this TLAC.dva has no Divaller support (it doesn't use WinUSB). "
             "Divaller support is only in PD-Loader dev builds (DEVd7dfc571 or newer), "
             "not in the 2.6.5a release. Shim inactive.");
        return 1;
    }
    return 1;
}

/* TLAC only looks for the Divaller on its first frame and after a
 * WM_DEVICECHANGE/DBT_DEVICEARRIVAL, which Wine never sends for it. So
 * whenever the bridge has a device that TLAC isn't holding (at startup, or
 * after TLAC dropped it because the bridge went away), send TLAC that message
 * ourselves to make it look again.
 *
 * It has to be *sent*, with a real DEV_BROADCAST record: Windows and Wine
 * treat DBT_DEVICEARRIVAL as a pointer message and refuse to post it
 * (ERROR_MESSAGE_SYNC_ONLY). */
static struct {
    DEV_BROADCAST_DEVICEINTERFACE_W hdr;
    WCHAR rest[sizeof(kFakePath) / sizeof(WCHAR)];
} g_arrival;

static void nudge_tlac(void)
{
    static int nudges, failures;
    HWND w = FindWindowW(L"MessageWindowClass", L"MessageWindowTitle");
    if (!w) return;  /* TLAC hasn't started its message thread yet */

    if (!g_arrival.hdr.dbcc_size) {
        g_arrival.hdr.dbcc_size = (DWORD)(FIELD_OFFSET(DEV_BROADCAST_DEVICEINTERFACE_W, dbcc_name) + sizeof(kFakePath));
        g_arrival.hdr.dbcc_devicetype = DBT_DEVTYP_DEVICEINTERFACE;
        g_arrival.hdr.dbcc_classguid = kUsbDeviceGuid;
        memcpy(g_arrival.hdr.dbcc_name, kFakePath, sizeof(kFakePath));
    }

    DWORD_PTR result;
    if (SendMessageTimeoutW(w, WM_DEVICECHANGE, DBT_DEVICEARRIVAL, (LPARAM)&g_arrival,
                            SMTO_ABORTIFHUNG | SMTO_NORMAL, 500, &result)) {
        nudges++;
        if (nudges <= 3 || nudges % 60 == 0)
            blog("asked TLAC to look for the Divaller (%d)", nudges);
    } else if (failures++ < 3) {
        blog("couldn't notify TLAC of the Divaller (error %u)", (unsigned)GetLastError());
    }
}

static DWORD WINAPI io_thread(LPVOID unused)
{
    (void)unused;
    /* PD-Loader loads plugins in directory order, so TLAC may come after us. */
    for (int i = 0; i < 600 && !try_patch(); i++) Sleep(100);
    if (!g_patched) {
        blog("TLAC.dva never loaded; shim inactive");
        return 0;
    }
    if (!g_has_divaller) return 0;

    SOCKET s = INVALID_SOCKET;
    int reported_fail = 0;
    DWORD last_settings = 0, last_nudge = 0;

    for (;;) {
        DWORD now = GetTickCount();

        /* Settings may be changed in the launcher after we load. */
        if (now - last_settings >= 2000) {
            last_settings = now;
            if (load_settings() && s != INVALID_SOCKET) {
                blog("bridge address changed to %ls:%d, reconnecting", g_host, (int)g_port);
                closesocket(s);
                s = INVALID_SOCKET;
                set_disconnected();
            }
        }

        if (s == INVALID_SOCKET) {
            s = bridge_connect();
            if (s == INVALID_SOCKET) {
                if (!reported_fail)
                    blog("bridge not reachable at %ls:%d (is divaller_bridge.py running there?)",
                         g_host, (int)g_port);
                reported_fail = 1;
                if (g_always && !g_handed) nudge_tlac();
                Sleep(1000);
                continue;
            }
            reported_fail = 0;
            EnterCriticalSection(&g_lock);
            g_connected = 1;
            LeaveCriticalSection(&g_lock);
            blog("connected to bridge at %ls:%d", g_host, (int)g_port);
        }

        if (!bridge_poll(s)) {
            blog(g_always ? "lost connection to bridge; TLAC sees an idle Divaller until it's back"
                          : "lost connection to bridge");
            closesocket(s);
            s = INVALID_SOCKET;
            set_disconnected();
            continue;
        }

        EnterCriticalSection(&g_lock);
        int present = g_present;
        LeaveCriticalSection(&g_lock);

        if ((present || g_always) && !g_handed && GetTickCount() - last_nudge >= 1000) {
            last_nudge = GetTickCount();
            nudge_tlac();
        }
        Sleep(present ? 1 : 100);
    }
}

static void read_config(void)
{
    load_settings();
    DWORD n;
    WCHAR t[MAX_PATH];
    n = GetEnvironmentVariableW(L"DIVALLER_BRIDGE_TARGET", t, MAX_PATH);
    if (n > 0 && n < MAX_PATH) memcpy(g_target, t, (n + 1) * sizeof(WCHAR));
}

/* ------------------------------------------------- PD-Loader launcher */

/* Mirrors PluginConfigApi.h from PD-Loader (packed, MSVC layout). */
#pragma pack(push, 1)
enum { CONFIG_BOOLEAN = 0, CONFIG_NUMERIC = 1, CONFIG_STRING = 2, CONFIG_GROUP_START = 7, CONFIG_GROUP_END = 8 };

struct PluginConfigBooleanData {
    LPCWSTR iniVarName, iniSectionName, iniFilePath, friendlyName, description;
    unsigned char defaultVal, saveAsString;
};

struct PluginConfigNumericData {
    LPCWSTR iniVarName, iniSectionName, iniFilePath, friendlyName, description;
    int defaultVal, minVal, maxVal;
};
struct PluginConfigStringData {
    LPCWSTR iniVarName, iniSectionName, iniFilePath, friendlyName, description;
    LPCWSTR defaultVal;
    unsigned char useUtf8;
};
struct PluginConfigGroupData { LPCWSTR name; int height; };
struct PluginConfigOption { int cfgType; void *data; };
struct PluginConfigArray { int len; struct PluginConfigOption *options; };
#pragma pack(pop)

static struct PluginConfigGroupData cfg_group = { L"Bridge connection", 145 };
static struct PluginConfigStringData cfg_host = {
    L"host", L"bridge", g_ini_path, L"Bridge address:",
    L"IP address or host name of the machine running divaller_bridge.py.\n"
    L"Use 127.0.0.1 when the Divaller is plugged into this machine.\n"
    L"For another machine, run the bridge there with --host 0.0.0.0.\n"
    L"Overridden by the DIVALLER_BRIDGE_HOST environment variable.",
    DEFAULT_HOST, 0 };
static struct PluginConfigNumericData cfg_port = {
    L"port", L"bridge", g_ini_path, L"Bridge port:",
    L"TCP port divaller_bridge.py listens on (its --port option).\n"
    L"Overridden by the DIVALLER_BRIDGE_PORT environment variable.",
    DEFAULT_PORT, 1, 65535 };

static struct PluginConfigBooleanData cfg_always = {
    L"always_connected", L"bridge", g_ini_path, L"Always connected",
    L"Keep the Divaller attached to TLAC even when the bridge or controller is\n"
    L"unavailable (it reads as nothing pressed), so it resumes instantly.\n"
    L"While on, TLAC takes the slider only from the Divaller: its keyboard and\n"
    L"DualShock 4 slider emulation are disabled. Turn off if you sometimes\n"
    L"play without the Divaller.",
    1, 0 };

static struct PluginConfigBooleanData cfg_background = {
    L"background_input", L"bridge", g_ini_path, L"Read Divaller while unfocused",
    L"Keep reading the Divaller (and XInput/DualShock 4 pads) when another\n"
    L"window has focus. Keyboard and mouse are ignored while the game is\n"
    L"in the background, so typing elsewhere doesn't reach the game.\n"
    L"TLAC's \"pause on focus loss\" won't trigger while this is on.",
    1, 0 };

static struct PluginConfigOption cfg_options[] = {
    { CONFIG_GROUP_START, &cfg_group },
    { CONFIG_STRING, &cfg_host },
    { CONFIG_NUMERIC, &cfg_port },
    { CONFIG_BOOLEAN, &cfg_always },
    { CONFIG_BOOLEAN, &cfg_background },
    { CONFIG_GROUP_END, NULL },
};

__declspec(dllexport) LPCWSTR GetPluginName(void) { return L"DivallerBridge"; }

__declspec(dllexport) LPCWSTR GetPluginDescription(void)
{
    return L"Connects TLAC's Divaller support to divaller_bridge.py, which owns the "
           L"real controller on Linux. Lets the Divaller work under Wine/Proton.";
}

__declspec(dllexport) LPCWSTR GetBuildDate(void) { return L"" __DATE__; }

__declspec(dllexport) struct PluginConfigArray GetPluginOptions(void)
{
    struct PluginConfigArray a = { (int)(sizeof(cfg_options) / sizeof(cfg_options[0])), cfg_options };
    return a;
}

/* PD-Loader calls this after loading the plugin. */
__declspec(dllexport) void InitializeDVA(void)
{
    try_patch();
}

BOOL WINAPI DllMain(HINSTANCE inst, DWORD reason, LPVOID reserved)
{
    (void)reserved;
    if (reason == DLL_PROCESS_ATTACH) {
        DisableThreadLibraryCalls(inst);
        InitializeCriticalSection(&g_lock);
        open_log(inst);
        sibling_path(inst, L"ini", g_ini_path);
        blog("loaded");
        read_config();
        blog("settings: bridge at %ls:%d", g_host, (int)g_port);
        HANDLE t = CreateThread(NULL, 0, io_thread, NULL, 0, NULL);
        if (t) CloseHandle(t);
    }
    return TRUE;
}
