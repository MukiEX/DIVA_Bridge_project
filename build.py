#!/usr/bin/env python3
"""
Build DIVA Bridge (GUI + bridge in one program) for the current OS.

    pip install -r requirements-build.txt
    python build.py

Output (in dist/):
    Windows   DIVA Bridge.exe                    single file
    Linux     diva-bridge                        single file
    macOS     DIVA Bridge.app                    app bundle (also zipped)
plus a release zip  dist/DIVA-Bridge-<version>-<os>-<arch>.zip  with the
program, README and (Linux) udev rules.

PyInstaller can't cross-compile: run this once on each OS, or let the GitHub
Actions workflow (.github/workflows/build.yml) build all three.

Options:
    --onedir        folder build instead of a single file (starts faster;
                    macOS always uses an .app bundle)
    --vendor DIR    extra native libraries to bundle (libusb-1.0.*,
                    hidapi.dll / libhidapi*.so / libhidapi.dylib). Default: ./vendor
    --no-zip        skip the release zip
    --debug-console keep a console window on Windows (shows tracebacks)
"""

import argparse
import glob
import importlib.util
import os
import platform
import shutil
import subprocess
import sys
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
APP_NAME = "DIVA Bridge"
LINUX_NAME = "diva-bridge"
VERSION = "1.0.0"
BUNDLE_ID = "io.github.divabridge"
NATIVE_PATTERNS = ("libusb-1.0*.dll", "libusb-1.0*.dylib", "libusb-1.0.so*",
                   "hidapi*.dll", "libhidapi*.dll", "libhidapi*.so*", "libhidapi*.dylib")


def os_tag():
    return {"win32": "windows", "darwin": "macos"}.get(sys.platform, "linux")


def arch_tag():
    m = platform.machine().lower()
    return {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(m, m)


def have(module):
    return importlib.util.find_spec(module) is not None


def check_environment():
    problems = []
    if sys.version_info < (3, 8):
        problems.append("Python 3.8 or newer is needed.")
    try:
        import tkinter  # noqa: F401
    except ImportError:
        problems.append("Tkinter is missing (Linux: install python3-tk / python3-tkinter / tk).")
    if not have("PyInstaller"):
        problems.append("PyInstaller is missing: pip install -r requirements-build.txt")
    if problems:
        sys.exit("Can't build:\n  " + "\n  ".join(problems))


def usb_libraries(vendor):
    """PyInstaller options for USB access, and a summary of what got included."""
    opts, notes = [], []
    if have("usb"):
        opts += ["--hidden-import", "usb.backend.libusb1", "--hidden-import", "usb.backend.libusb0"]
        notes.append("pyusb")
    else:
        notes.append("NO pyusb (Divaller won't work)")
    if have("libusb_package"):
        opts += ["--collect-all", "libusb_package"]
        notes.append("libusb (libusb-package wheel)")
    vendor_hid = [p for pat in ("hidapi*.dll", "libhidapi*") for p in glob.glob(os.path.join(vendor, pat))]
    if have("hid"):
        opts += ["--hidden-import", "hid"]
        try:
            import hid
            kind = "cython-hidapi" if hasattr(hid, "device") else "ctypes hid + system hidapi library"
        except Exception:
            kind = ("ctypes hid + hidapi library from vendor/" if vendor_hid
                    else "ctypes hid, but NO hidapi library found - put one in vendor/")
        notes.append(f"hidapi ({kind})")
    else:
        notes.append("NO hidapi (Windows/macOS will need libusb + WinUSB for HID controllers)")
    for pattern in NATIVE_PATTERNS:
        for path in glob.glob(os.path.join(vendor, pattern)):
            opts += ["--add-binary", f"{path}{os.pathsep}."]
            notes.append(f"vendor/{os.path.basename(path)}")
    return opts, notes


def icon_option():
    if sys.platform == "win32":
        ico = os.path.join(HERE, "assets", "icon.ico")
        return ["--icon", ico] if os.path.exists(ico) else []
    if sys.platform == "darwin":
        icns = os.path.join(HERE, "assets", "icon.icns")
        png = os.path.join(HERE, "assets", "icon.png")
        if os.path.exists(icns):
            return ["--icon", icns]
        if os.path.exists(png) and have("PIL"):       # PyInstaller converts PNG with Pillow
            return ["--icon", png]
    return []


def build(args):
    check_environment()
    import PyInstaller.__main__

    is_mac, is_win = sys.platform == "darwin", sys.platform == "win32"
    name = LINUX_NAME if os_tag() == "linux" else APP_NAME
    usb_opts, notes = usb_libraries(args.vendor)
    print(f"Building {APP_NAME} {VERSION} for {os_tag()}-{arch_tag()}")
    print("  including: " + ", ".join(notes))

    opts = [
        os.path.join(HERE, "diva_bridge_gui.py"),
        "--name", name,
        "--noconfirm", "--clean",
        "--distpath", os.path.join(HERE, "dist"),
        "--workpath", os.path.join(HERE, "build"),
        "--specpath", os.path.join(HERE, "build"),
        "--paths", HERE,
        "--hidden-import", "diva_bridge",
        "--add-data", f"{os.path.join(HERE, 'assets', 'icon.png')}{os.pathsep}assets",
    ] + usb_opts + icon_option()

    if is_mac:
        # A .app must be a folder build; --onefile + --windowed is deprecated on macOS.
        opts += ["--windowed", "--onedir", "--osx-bundle-identifier", BUNDLE_ID]
    else:
        opts += ["--onedir" if args.onedir else "--onefile"]
        if is_win and not args.debug_console:
            opts += ["--windowed"]

    PyInstaller.__main__.run(opts)

    dist = os.path.join(HERE, "dist")
    if is_mac:
        product = os.path.join(dist, APP_NAME + ".app")
    elif args.onedir:
        product = os.path.join(dist, name)
    else:
        product = os.path.join(dist, name + (".exe" if is_win else ""))
    if not os.path.exists(product):
        sys.exit(f"Build finished but {product} is missing")
    print(f"\nBuilt: {product}")

    if not args.no_zip:
        make_release_zip(product, dist)


def make_release_zip(product, dist):
    zip_name = f"DIVA-Bridge-{VERSION}-{os_tag()}-{arch_tag()}.zip"
    zip_path = os.path.join(dist, zip_name)
    extras = ["README.md"] + (["70-diva-controllers.rules"] if os_tag() == "linux" else [])
    if sys.platform == "darwin":
        # ditto keeps the .app's symlinks, permissions and signatures intact.
        staging = os.path.join(dist, "release")
        shutil.rmtree(staging, ignore_errors=True)
        os.makedirs(staging)
        subprocess.run(["ditto", product, os.path.join(staging, os.path.basename(product))], check=True)
        for e in extras:
            if os.path.exists(os.path.join(HERE, e)):
                shutil.copy(os.path.join(HERE, e), staging)
        if os.path.exists(zip_path):
            os.remove(zip_path)
        subprocess.run(["ditto", "-c", "-k", "--sequesterRsrc", staging, zip_path], check=True)
    else:
        with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as z:
            if os.path.isdir(product):
                for root, _, files in os.walk(product):
                    for f in files:
                        full = os.path.join(root, f)
                        z.write(full, os.path.relpath(full, dist))
            else:
                info = zipfile.ZipInfo.from_file(product, os.path.basename(product))
                info.external_attr = 0o755 << 16          # keep the Linux binary executable
                with open(product, "rb") as fh:
                    z.writestr(info, fh.read(), zipfile.ZIP_DEFLATED)
            for e in extras:
                if os.path.exists(os.path.join(HERE, e)):
                    z.write(os.path.join(HERE, e), e)
    print(f"Release: {zip_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--onedir", action="store_true", help="folder build instead of a single file")
    ap.add_argument("--vendor", default=os.path.join(HERE, "vendor"), help="folder of extra native libraries")
    ap.add_argument("--no-zip", action="store_true", help="don't make the release zip")
    ap.add_argument("--debug-console", action="store_true", help="Windows: keep a console window")
    build(ap.parse_args())


if __name__ == "__main__":
    main()
