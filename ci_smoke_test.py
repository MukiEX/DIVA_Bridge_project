#!/usr/bin/env python3
"""Start the built program in bridge mode and check it answers on its port.
Used by CI after build.py; also handy locally (python ci_smoke_test.py)."""
import glob, os, socket, subprocess, sys, time

dist = os.path.join(os.path.dirname(os.path.abspath(__file__)), "dist")
if sys.platform == "darwin":
    exe = os.path.join(dist, "DIVA Bridge.app", "Contents", "MacOS", "DIVA Bridge")
elif sys.platform == "win32":
    exe = os.path.join(dist, "DIVA Bridge.exe")
else:
    exe = os.path.join(dist, "diva-bridge")
if not os.path.exists(exe):                      # --onedir builds
    found = glob.glob(os.path.join(dist, "*", os.path.basename(exe)))
    exe = found[0] if found else exe
port = 45799
logf = os.path.join(dist, "smoke_test.log")
out_fh = open(logf, "w")
kw = {"start_new_session": True} if sys.platform != "win32" else {}
proc = subprocess.Popen([exe, "--bridge", "--port", str(port), "--parent-pid", str(os.getpid())],
                        stdout=out_fh, stderr=subprocess.STDOUT, **kw)
ok = False
for _ in range(60):
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=1) as s:
            s.sendall(b"P")
            ok = s.recv(1) in (b"\x00", b"\x01")
            break
    except OSError:
        time.sleep(1)
if sys.platform == "win32":
    subprocess.run(["taskkill", "/PID", str(proc.pid), "/T", "/F"], capture_output=True)
else:
    import signal
    os.killpg(proc.pid, signal.SIGTERM)
try:
    proc.wait(timeout=10)
except subprocess.TimeoutExpired:
    proc.kill()
out_fh.close()
out = open(logf, encoding="utf-8", errors="replace").read()
print(out)
if not ok:
    sys.exit("bridge mode did not answer")
if "USB access available: none" in out:
    sys.exit("no USB library was bundled")
print("smoke test passed")
