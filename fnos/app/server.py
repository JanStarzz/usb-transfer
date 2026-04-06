#!/usr/bin/env python3
"""USB Transfer - fnOS USB storage file transfer service with DCIM auto-import."""

import argparse
import json
import os
import re
import signal
import subprocess
import threading
import time
from http.server import HTTPServer, SimpleHTTPRequestHandler
from pathlib import Path
from urllib.parse import parse_qs, urlparse

# ---------------------------------------------------------------------------
# Global state
# ---------------------------------------------------------------------------
transfer_lock = threading.Lock()
current_transfer = {
    "active": False,
    "pid": None,
    "process": None,
    "src": "",
    "dst": "",
    "mode": "copy",
    "progress": 0,
    "speed": "",
    "transferred": "",
    "total_files": 0,
    "current_file": "",
    "done_files": 0,
    "started_at": 0,
    "error": None,
}

DATA_DIR = "/tmp"
HISTORY_FILE = ""
SETTINGS_FILE = ""

# Auto-detection state
auto_events_lock = threading.Lock()
auto_events = []  # [{timestamp, type, message}]
MAX_AUTO_EVENTS = 50


# ---------------------------------------------------------------------------
# Settings persistence
# ---------------------------------------------------------------------------
def load_settings():
    defaults = {"dest_path": "", "auto_transfer": False, "source_dirs": ["DCIM"]}
    if not SETTINGS_FILE or not os.path.exists(SETTINGS_FILE):
        return defaults
    try:
        with open(SETTINGS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
            return {
                "dest_path": data.get("dest_path", ""),
                "auto_transfer": data.get("auto_transfer", False),
                "source_dirs": data.get("source_dirs", ["DCIM"]),
            }
    except Exception:
        return defaults


def save_settings(settings):
    if not SETTINGS_FILE:
        return
    os.makedirs(os.path.dirname(SETTINGS_FILE), exist_ok=True)
    with open(SETTINGS_FILE, "w", encoding="utf-8") as f:
        json.dump(settings, f, ensure_ascii=False, indent=2)


def add_auto_event(event_type, message):
    with auto_events_lock:
        auto_events.append({
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
            "type": event_type,
            "message": message,
        })
        if len(auto_events) > MAX_AUTO_EVENTS:
            del auto_events[: len(auto_events) - MAX_AUTO_EVENTS]


# ---------------------------------------------------------------------------
# History
# ---------------------------------------------------------------------------
def load_history():
    if not HISTORY_FILE or not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_history(records):
    if not HISTORY_FILE:
        return
    os.makedirs(os.path.dirname(HISTORY_FILE), exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(records[-100:], f, ensure_ascii=False, indent=2)


# ---------------------------------------------------------------------------
# USB detection
# ---------------------------------------------------------------------------
def get_usb_devices():
    """Detect mounted USB storage devices via lsblk."""
    devices = []
    try:
        out = subprocess.check_output(
            ["lsblk", "-J", "-o", "NAME,SIZE,TYPE,MOUNTPOINT,TRAN,LABEL,FSTYPE,MODEL"],
            text=True,
            timeout=5,
        )
        data = json.loads(out)
        for dev in data.get("blockdevices", []):
            _collect_usb(dev, devices)
    except Exception:
        # Fallback: parse /proc/mounts for common USB mount paths
        try:
            with open("/proc/mounts", "r") as f:
                for line in f:
                    parts = line.split()
                    if len(parts) >= 2:
                        dev, mount = parts[0], parts[1]
                        if "/sd" in dev and mount.startswith(("/media", "/mnt", "/vol")):
                            devices.append({
                                "name": os.path.basename(dev),
                                "mountpoint": mount,
                                "size": "",
                                "label": os.path.basename(mount),
                                "fstype": parts[2] if len(parts) > 2 else "",
                                "model": "",
                            })
        except Exception:
            pass
    return devices


def _collect_usb(dev, out, parent_is_usb=False):
    is_usb = dev.get("tran") == "usb" or parent_is_usb
    if is_usb and dev.get("mountpoint"):
        out.append({
            "name": dev.get("name", ""),
            "mountpoint": dev["mountpoint"],
            "size": dev.get("size", ""),
            "label": dev.get("label") or dev.get("name", ""),
            "fstype": dev.get("fstype", ""),
            "model": dev.get("model", ""),
        })
    for child in dev.get("children", []):
        _collect_usb(child, out, is_usb)


def find_source_dirs(mountpoint):
    """Search for configured source directories on a USB device (case-insensitive)."""
    settings = load_settings()
    target_names = [d.upper() for d in settings.get("source_dirs", ["DCIM"])]
    found = []
    try:
        for entry in os.scandir(mountpoint):
            if entry.is_dir() and entry.name.upper() in target_names:
                found.append(entry.path)
    except Exception:
        pass
    return found


# ---------------------------------------------------------------------------
# USB auto-watcher thread
# ---------------------------------------------------------------------------
def usb_watcher_thread():
    """Background thread that polls for new USB devices and auto-imports DCIM."""
    known_mounts = set()
    # Initialize with currently connected devices
    try:
        for dev in get_usb_devices():
            known_mounts.add(dev["mountpoint"])
    except Exception:
        pass

    while True:
        try:
            time.sleep(3)
            current_devices = get_usb_devices()
            current_mounts = {d["mountpoint"] for d in current_devices}

            # Detect removed devices
            removed = known_mounts - current_mounts
            for mp in removed:
                add_auto_event("removed", f"USB 设备已拔出: {mp}")
            known_mounts -= removed

            # Detect new devices
            new_mounts = current_mounts - known_mounts
            for dev in current_devices:
                if dev["mountpoint"] not in new_mounts:
                    continue

                label = dev.get("label") or dev.get("name") or dev["mountpoint"]
                add_auto_event("detected", f"检测到新USB设备: {label} ({dev['mountpoint']})")

                # Search for configured source directories
                found_dirs = find_source_dirs(dev["mountpoint"])
                if found_dirs:
                    dir_names = ", ".join(os.path.basename(d) for d in found_dirs)
                    add_auto_event("dcim_found", f"发现目录: {dir_names} ({dev['mountpoint']})")

                    settings = load_settings()
                    if settings["auto_transfer"] and settings["dest_path"]:
                        dest = settings["dest_path"]
                        for src_dir in found_dirs:
                            with transfer_lock:
                                busy = current_transfer["active"]
                            if busy:
                                add_auto_event("skipped", f"传输队列繁忙，跳过: {src_dir}")
                                break
                            add_auto_event("auto_start", f"自动导入: {src_dir} -> {dest}")
                            result = start_transfer(src_dir, dest, "sync")
                            if "error" in result:
                                add_auto_event("error", f"导入失败: {result['error']}")
                            else:
                                add_auto_event("transferring", f"正在传输 {result.get('total_files', 0)} 个文件...")
                                # Wait for this transfer to finish before starting next
                                while True:
                                    time.sleep(1)
                                    with transfer_lock:
                                        if not current_transfer["active"]:
                                            break
                    elif not settings["dest_path"]:
                        add_auto_event("need_config", f"发现 {dir_names} 但未设置目标目录，请在Web界面中设置")
                    else:
                        add_auto_event("disabled", "自动传输已关闭，如需自动导入请在设置中开启")
                else:
                    src_names = ", ".join(settings.get("source_dirs", ["DCIM"]))
                    add_auto_event("no_dcim", f"设备 {label} 上未发现 {src_names} 目录")

                known_mounts.add(dev["mountpoint"])

        except Exception as e:
            add_auto_event("error", f"监听线程异常: {str(e)}")
            time.sleep(5)


# ---------------------------------------------------------------------------
# Directory browsing
# ---------------------------------------------------------------------------
def browse_directory(path):
    """List directory contents with metadata."""
    path = os.path.realpath(path)
    if not os.path.isdir(path):
        return {"error": f"Not a directory: {path}"}

    items = []
    try:
        for entry in sorted(os.scandir(path), key=lambda e: (not e.is_dir(), e.name.lower())):
            try:
                stat = entry.stat(follow_symlinks=False)
                items.append({
                    "name": entry.name,
                    "is_dir": entry.is_dir(follow_symlinks=False),
                    "size": stat.st_size if not entry.is_dir() else 0,
                    "mtime": int(stat.st_mtime),
                })
            except PermissionError:
                items.append({
                    "name": entry.name,
                    "is_dir": entry.is_dir(follow_symlinks=False),
                    "size": 0,
                    "mtime": 0,
                })
    except PermissionError:
        return {"error": f"Permission denied: {path}"}

    return {"path": path, "items": items}


def get_nas_shares():
    """Get NAS shared directories (typical fnOS volume paths)."""
    shares = []
    for vol_entry in sorted(Path("/").glob("vol*")):
        if vol_entry.is_dir():
            shares.append({
                "name": vol_entry.name,
                "path": str(vol_entry),
            })
            try:
                for sub in sorted(vol_entry.iterdir()):
                    if sub.is_dir() and not sub.name.startswith(".") and not sub.name.startswith("@"):
                        shares.append({
                            "name": f"{vol_entry.name}/{sub.name}",
                            "path": str(sub),
                        })
            except PermissionError:
                pass
    for p in ["/media", "/mnt"]:
        if os.path.isdir(p):
            shares.append({"name": os.path.basename(p), "path": p})
    return shares


# ---------------------------------------------------------------------------
# File transfer (rsync-based)
# ---------------------------------------------------------------------------
def count_files(path):
    """Count files recursively for progress tracking."""
    count = 0
    try:
        for _, _, files in os.walk(path):
            count += len(files)
    except Exception:
        pass
    return count


def start_transfer(src, dst, mode="copy"):
    """Start an rsync-based file transfer in a background thread."""
    global current_transfer

    with transfer_lock:
        if current_transfer["active"]:
            return {"error": "A transfer is already in progress"}

    src = os.path.realpath(src)
    dst = os.path.realpath(dst)

    if not os.path.exists(src):
        return {"error": f"Source path does not exist: {src}"}
    if not os.path.isdir(dst):
        try:
            os.makedirs(dst, exist_ok=True)
        except Exception as e:
            return {"error": f"Cannot create destination: {e}"}

    if not src.endswith("/"):
        src += "/"

    total = count_files(src.rstrip("/"))

    with transfer_lock:
        current_transfer.update({
            "active": True,
            "pid": None,
            "process": None,
            "src": src,
            "dst": dst,
            "mode": mode,
            "progress": 0,
            "speed": "",
            "transferred": "",
            "total_files": total,
            "current_file": "",
            "done_files": 0,
            "started_at": time.time(),
            "error": None,
        })

    thread = threading.Thread(target=_run_transfer, args=(src, dst, mode), daemon=True)
    thread.start()
    return {"status": "started", "total_files": total}


def _run_transfer(src, dst, mode):
    global current_transfer

    cmd = ["rsync", "-ah", "--info=progress2,name1", "--no-inc-recursive"]

    if mode == "sync":
        cmd.append("--update")
    elif mode == "mirror":
        cmd.append("--delete")

    cmd += [src, dst]

    try:
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        with transfer_lock:
            current_transfer["pid"] = proc.pid
            current_transfer["process"] = proc

        done_files = 0
        for line in proc.stdout:
            line = line.strip()
            if not line:
                continue

            m = re.search(
                r"([\d.,]+[KMGT]?i?B?)\s+(\d+)%\s+([\d.,]+[KMGT]?i?B?/s)\s+(\S+)",
                line,
            )
            if m:
                with transfer_lock:
                    current_transfer["transferred"] = m.group(1)
                    current_transfer["progress"] = int(m.group(2))
                    current_transfer["speed"] = m.group(3)
                continue

            if not line.startswith(" ") and "%" not in line and "/" not in line[:3]:
                done_files += 1
                with transfer_lock:
                    current_transfer["current_file"] = line
                    current_transfer["done_files"] = done_files

        proc.wait()
        rc = proc.returncode

        elapsed = time.time() - current_transfer["started_at"]

        record = {
            "src": src,
            "dst": dst,
            "mode": mode,
            "total_files": current_transfer["total_files"],
            "done_files": current_transfer["done_files"],
            "success": rc == 0,
            "error": f"rsync exited with code {rc}" if rc != 0 else None,
            "elapsed": round(elapsed, 1),
            "timestamp": time.strftime("%Y-%m-%d %H:%M:%S"),
        }

        history = load_history()
        history.append(record)
        save_history(history)

        with transfer_lock:
            if rc == 0:
                current_transfer["progress"] = 100
                add_auto_event("done", f"传输完成: {src} -> {dst}")
            else:
                current_transfer["error"] = f"rsync exited with code {rc}"
                add_auto_event("error", f"传输失败 (code {rc}): {src}")
            current_transfer["active"] = False
            current_transfer["process"] = None

    except Exception as e:
        with transfer_lock:
            current_transfer["error"] = str(e)
            current_transfer["active"] = False
            current_transfer["process"] = None
        add_auto_event("error", f"传输异常: {str(e)}")


def cancel_transfer():
    global current_transfer
    with transfer_lock:
        proc = current_transfer.get("process")
        if proc and current_transfer["active"]:
            try:
                proc.terminate()
                proc.wait(timeout=5)
            except Exception:
                try:
                    proc.kill()
                except Exception:
                    pass
            current_transfer["active"] = False
            current_transfer["error"] = "Cancelled by user"
            current_transfer["process"] = None
            return {"status": "cancelled"}
    return {"status": "no_active_transfer"}


# ---------------------------------------------------------------------------
# HTTP Handler
# ---------------------------------------------------------------------------
class USBTransferHandler(SimpleHTTPRequestHandler):
    static_dir = ""

    def log_message(self, format, *args):
        pass

    def _json(self, data, status=200):
        body = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", len(body))
        self.end_headers()
        self.wfile.write(body)

    def _read_body(self):
        length = int(self.headers.get("Content-Length", 0))
        if length == 0:
            return {}
        raw = self.rfile.read(length)
        return json.loads(raw)

    def do_GET(self):
        parsed = urlparse(self.path)
        path = parsed.path
        qs = parse_qs(parsed.query)

        if path == "/api/usb-devices":
            self._json(get_usb_devices())
        elif path == "/api/browse":
            dir_path = qs.get("path", ["/"])[0]
            self._json(browse_directory(dir_path))
        elif path == "/api/transfer/status":
            with transfer_lock:
                safe = {k: v for k, v in current_transfer.items() if k not in ("process",)}
            self._json(safe)
        elif path == "/api/transfer/history":
            self._json(load_history())
        elif path == "/api/nas-shares":
            self._json(get_nas_shares())
        elif path == "/api/settings":
            self._json(load_settings())
        elif path == "/api/auto-events":
            with auto_events_lock:
                self._json(list(auto_events))
        elif path == "/" or path == "/index.html":
            index_path = os.path.join(self.static_dir, "index.html")
            try:
                with open(index_path, "rb") as f:
                    content = f.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", len(content))
                self.end_headers()
                self.wfile.write(content)
            except FileNotFoundError:
                self.send_error(404, "index.html not found")
        else:
            file_path = os.path.join(self.static_dir, path.lstrip("/"))
            if os.path.isfile(file_path):
                self.path = path
                self.directory = self.static_dir
                super().do_GET()
            else:
                self.send_error(404)

    def do_POST(self):
        parsed = urlparse(self.path)
        path = parsed.path

        if path == "/api/transfer":
            body = self._read_body()
            src = body.get("src", "")
            dst = body.get("dst", "")
            mode = body.get("mode", "copy")
            if not src or not dst:
                self._json({"error": "src and dst are required"}, 400)
                return
            result = start_transfer(src, dst, mode)
            status = 400 if "error" in result else 200
            self._json(result, status)
        elif path == "/api/transfer/cancel":
            self._json(cancel_transfer())
        elif path == "/api/settings":
            body = self._read_body()
            settings = load_settings()
            if "dest_path" in body:
                settings["dest_path"] = body["dest_path"]
            if "auto_transfer" in body:
                settings["auto_transfer"] = bool(body["auto_transfer"])
            if "source_dirs" in body:
                dirs = body["source_dirs"]
                if isinstance(dirs, list):
                    settings["source_dirs"] = [d.strip() for d in dirs if d.strip()]
            save_settings(settings)
            dirs_str = ", ".join(settings["source_dirs"])
            add_auto_event("config", f"设置已更新: 监听目录={dirs_str}, 目标={settings['dest_path']}, 自动={settings['auto_transfer']}")
            self._json(settings)
        else:
            self.send_error(404)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------
def main():
    global DATA_DIR, HISTORY_FILE, SETTINGS_FILE

    parser = argparse.ArgumentParser(description="USB Transfer Server")
    parser.add_argument("--port", type=int, default=8580)
    parser.add_argument("--data-dir", default="/tmp/usb-transfer")
    parser.add_argument("--log-dir", default="/tmp/usb-transfer")
    args = parser.parse_args()

    DATA_DIR = args.data_dir
    HISTORY_FILE = os.path.join(args.data_dir, "history", "transfers.json")
    SETTINGS_FILE = os.path.join(args.data_dir, "settings.json")
    os.makedirs(os.path.join(args.data_dir, "history"), exist_ok=True)

    # Start USB watcher thread
    watcher = threading.Thread(target=usb_watcher_thread, daemon=True)
    watcher.start()
    add_auto_event("started", "USB 监听服务已启动")

    script_dir = os.path.dirname(os.path.abspath(__file__))
    USBTransferHandler.static_dir = os.path.join(script_dir, "static")
    USBTransferHandler.directory = USBTransferHandler.static_dir

    server = HTTPServer(("0.0.0.0", args.port), USBTransferHandler)
    print(f"USB Transfer server running on port {args.port}")

    def shutdown(sig, frame):
        cancel_transfer()
        server.shutdown()

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()


if __name__ == "__main__":
    main()
