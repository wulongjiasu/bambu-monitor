#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Bambu Lab LAN monitor.

One MQTT-over-TLS client per printer, an HTTP server for the dashboard and
Server-Sent Events (SSE) to push new states to every open browser tab.

Usage:
    python3 server.py --mock          # demo with 20 simulated printers
    python3 server.py                 # read config.json
"""

from __future__ import annotations

import argparse
import ipaddress
import json
import logging
import mimetypes
import os
import queue
import random
import re
import select
import signal
import socket
import ssl
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from itertools import count
from pathlib import Path
from urllib.parse import unquote, urlparse

try:
    import paho.mqtt.client as mqtt
except ImportError:  # pragma: no cover - handled at startup
    mqtt = None

BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"

LOG = logging.getLogger("bambu-monitor")

MQTT_PORT = 8883
REPORT_TOPIC = "device/{serial}/report"
REQUEST_TOPIC = "device/{serial}/request"

# 拓竹打印机用非标准 SSDP 端口做局域网广播（1990 / 2021）。
SSDP_GROUP = "239.255.255.250"
SSDP_PORTS = (1990, 2021)
SSDP_TARGET = "urn:bambulab-com:device:3dprinter:1"
SERIAL_RE = re.compile(r"^[0-9A-Z]{15,18}$")

MODEL_CODES = {
    "BLP001": "X1 Carbon",
    "BLP002": "X1",
    "BLP003": "X1E",
    "C11": "P1P",
    "C12": "P1S",
    "C13": "X1E",
    "N1": "A1 mini",
    "N2S": "A1",
    "O1D": "H2D",
    "O1E": "H2D Pro",
    "O2D": "H2S",
    "X1C": "X1 Carbon",
    "X1": "X1",
    "X1E": "X1E",
    "X2D": "X2D",
    "A1MINI": "A1 mini",
    "A1": "A1",
    "P1S": "P1S",
    "P1P": "P1P",
    "P2S": "P2S",
    "H2D": "H2D",
    "H2C": "H2C",
    "H2S": "H2S",
}

MODEL_HINTS = (
    ("x1 carbon", "X1 Carbon"),
    ("x1carbon", "X1 Carbon"),
    ("x1c", "X1 Carbon"),
    ("x1e", "X1E"),
    ("x1", "X1"),
    ("p1s", "P1S"),
    ("p1p", "P1P"),
    ("a1 mini", "A1 mini"),
    ("a1mini", "A1 mini"),
    ("a1", "A1"),
    ("h2d pro", "H2D Pro"),
    ("h2d", "H2D"),
    ("h2c", "H2C"),
    ("h2s", "H2S"),
    ("x2d", "X2D"),
    ("p2s", "P2S"),
)

STATUS_LABELS = {
    "IDLE": "待机",
    "PREPARE": "准备中",
    "RUNNING": "打印中",
    "PAUSE": "已暂停",
    "FINISH": "已完成",
    "FAILED": "失败",
    "SLICING": "切片中",
    "INIT": "初始化",
    "OFFLINE": "离线",
}

SPEED_LABELS = {1: "静音", 2: "标准", 3: "运动", 4: "狂暴"}

# 这些状态下机器仍在执行任务，计入累计工时（运行 + 暂停 + 准备）。
WORKING_STATES = {"PREPARE", "RUNNING", "PAUSE"}

DEFAULT_STATE = {
    "gcode_state": "IDLE",
    "mc_percent": 0,
    "mc_remaining_time": 0,
    "layer_num": 0,
    "total_layer_num": 0,
    "subtask_name": "",
    "gcode_file": "",
    "nozzle_temper": 0.0,
    "nozzle_target_temper": 0.0,
    "bed_temper": 0.0,
    "bed_target_temper": 0.0,
    "chamber_temper": None,
    "cooling_fan_speed": None,
    "heatbreak_fan_speed": None,
    "aux_fan_speed": None,
    "chamber_fan_speed": None,
    # Legacy/canonical Bambu report keys for aux + chamber fans.
    "big_fan1_speed": None,
    "big_fan2_speed": None,
    "wifi_signal": None,
    "print_error": 0,
    "spd_lvl": None,
    "task_id": "",
}


def _as_float(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def _as_int(value):
    try:
        return int(float(value))
    except (TypeError, ValueError):
        return None


def _first_int(*values):
    for value in values:
        parsed = _as_int(value)
        if parsed is not None:
            return parsed
    return None


def _parse_dbm(value):
    """Bambu reports wifi_signal either as a number or as '-52dBm'."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value)
    match = re.search(r"-?\d+", str(value))
    return int(match.group()) if match else None


def _now_ms() -> int:
    return int(time.time() * 1000)


# --------------------------------------------------------------- discovery
def _clean_model(raw: str) -> str:
    """Turn Bambu's discovery model string into a display name."""
    value = str(raw or "").strip()
    if not value:
        return ""
    code = re.sub(r"[^0-9A-Za-z]", "", value).upper()
    if code in MODEL_CODES:
        return MODEL_CODES[code]
    low = value.lower().replace("_", " ").replace("-", " ").replace("3dprinter ", "")
    for needle, label in MODEL_HINTS:
        if needle in low:
            return label
    # Unknown model: keep it readable instead of showing a raw code.
    return value.replace("3DPrinter-", "").replace("3dprinter-", "").replace("-", " ").strip() or value


def _looks_like_serial(value: str) -> bool:
    return bool(SERIAL_RE.fullmatch(str(value or "").strip().upper()))


def _valid_ipv4(value: str) -> str:
    try:
        return str(ipaddress.IPv4Address(str(value).strip()))
    except (ipaddress.AddressValueError, ValueError):
        return ""


def _parse_ssdp_headers(text: str) -> dict[str, str]:
    headers: dict[str, str] = {}
    lines = text.replace("\r\n", "\n").split("\n")
    for line in lines[1:]:
        if ":" not in line:
            continue
        key, _, value = line.partition(":")
        headers[key.strip().lower()] = value.strip()
    return headers


def _absorb_ssdp(data: bytes, sender: tuple[str, int], found: dict[str, dict]) -> None:
    text = data.decode("utf-8", "replace")
    first_line = text.split("\n", 1)[0].strip().upper()
    # 自己的 M-SEARCH 会被组播回环收回来，跳过请求报文。
    if first_line.startswith("M-SEARCH"):
        return
    low = text.lower()
    if "bambulab" not in low and "devmodel" not in low and "devname" not in low:
        return
    headers = _parse_ssdp_headers(text)
    if not any(key in headers for key in ("devmodel.bambu.com", "devname.bambu.com", "usn")):
        return

    ip = headers.get("location") or sender[0]
    ip = re.sub(r"^[a-z]+://", "", str(ip), flags=re.I).split("/")[0]
    ip = _valid_ipv4(ip.split(":")[0]) or _valid_ipv4(sender[0])
    if not ip:
        return

    serial = ""
    for key in ("usn", "devserial.bambu.com", "serial.bambu.com", "serialnumber"):
        value = str(headers.get(key) or "").strip()
        if _looks_like_serial(value):
            serial = value.upper()
            break
    if not serial:
        for key, value in headers.items():
            if "serial" in key and _looks_like_serial(value):
                serial = value.upper()
                break

    model = _clean_model(headers.get("devmodel.bambu.com") or headers.get("devmodel") or "")
    name = str(headers.get("devname.bambu.com") or headers.get("devname") or "").strip()

    signal = _as_int(headers.get("devsignal.bambu.com") or headers.get("devsignal"))

    key = serial or ip
    entry = found.get(key)
    if entry is None:
        entry = {
            "ip": ip,
            "serial": serial,
            "model": model,
            "name": name,
            "signal": signal,
            "source": "ssdp",
        }
        found[key] = entry
    else:
        entry["ip"] = ip or entry["ip"]
        entry["serial"] = serial or entry["serial"]
        entry["model"] = model or entry["model"]
        entry["name"] = name or entry["name"]
        if signal is not None:
            entry["signal"] = signal


def _ssdp_scan(timeout: float = 4.0) -> dict[str, dict]:
    """Listen for Bambu SSDP announcements and probe with M-SEARCH."""
    found: dict[str, dict] = {}
    listeners: list[socket.socket] = []
    for port in (0, *SSDP_PORTS):
        sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
        try:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            if hasattr(socket, "SO_REUSEPORT"):
                try:
                    sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEPORT, 1)
                except OSError:
                    pass
            sock.bind(("", port))
            if port:
                # 加入拓竹使用的组播组，才能收到 NOTIFY 广播。
                request = socket.inet_aton(SSDP_GROUP) + socket.inet_aton("0.0.0.0")
                sock.setsockopt(socket.IPPROTO_IP, socket.IP_ADD_MEMBERSHIP, request)
            sock.setblocking(False)
            listeners.append(sock)
        except OSError as exc:
            sock.close()
            LOG.debug("SSDP 监听端口 %s 失败: %s", port or "临时端口", exc)

    sender = socket.socket(socket.AF_INET, socket.SOCK_DGRAM, socket.IPPROTO_UDP)
    sender.setsockopt(socket.SOL_SOCKET, socket.SO_BROADCAST, 1)
    try:
        sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_TTL, 2)
        sender.setsockopt(socket.IPPROTO_IP, socket.IP_MULTICAST_LOOP, 1)
    except OSError:
        pass

    search = (
        "M-SEARCH * HTTP/1.1\r\n"
        f"HOST: {SSDP_GROUP}:2021\r\n"
        'MAN: "ssdp:discover"\r\n'
        "MX: 1\r\n"
        f"ST: {SSDP_TARGET}\r\n"
        "\r\n"
    ).encode("ascii")

    deadline = time.monotonic() + timeout
    next_search = 0.0
    try:
        while time.monotonic() < deadline:
            now = time.monotonic()
            if now >= next_search:
                for target in (
                    (SSDP_GROUP, 2021),
                    (SSDP_GROUP, 1990),
                    ("255.255.255.255", 2021),
                    ("255.255.255.255", 1990),
                ):
                    try:
                        sender.sendto(search, target)
                    except OSError:
                        pass
                next_search = now + 1.2

            readable, _, _ = select.select(listeners, [], [], 0.2)
            for sock in readable:
                while True:
                    try:
                        data, addr = sock.recvfrom(8192)
                    except (BlockingIOError, InterruptedError):
                        break
                    except OSError:
                        break
                    _absorb_ssdp(data, addr, found)
    finally:
        for sock in listeners:
            sock.close()
        sender.close()
    return found


def _der_common_names(der: bytes) -> list[str]:
    """Extract CN (OID 2.5.4.3) values from a DER certificate."""
    names: list[str] = []
    oid = b"\x06\x03\x55\x04\x03"
    index = 0
    while True:
        index = der.find(oid, index)
        if index < 0:
            break
        pos = index + len(oid)
        index = pos
        if pos + 2 > len(der):
            break
        tag = der[pos]
        if tag not in (0x0C, 0x13, 0x16, 0x14):  # UTF8String / PrintableString / IA5String / T61
            continue
        length = der[pos + 1]
        start = pos + 2
        if length & 0x80:
            count = length & 0x7F
            if not 1 <= count <= 4 or start + count > len(der):
                continue
            length = int.from_bytes(der[start:start + count], "big")
            start += count
        if start + length > len(der):
            continue
        names.append(der[start:start + length].decode("utf-8", "replace").strip())
    return names


def _tls_serial(ip: str, timeout: float = 1.6) -> str:
    """Bambu's MQTT certificate CN is the printer serial number."""
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    context.check_hostname = False
    context.verify_mode = ssl.CERT_NONE
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    context.maximum_version = ssl.TLSVersion.TLSv1_2
    try:
        with socket.create_connection((ip, MQTT_PORT), timeout=timeout) as raw:
            with context.wrap_socket(raw, server_hostname=None) as tls:
                der = tls.getpeercert(binary_form=True)
    except (OSError, ssl.SSLError):
        return ""
    if not der:
        return ""
    for name in _der_common_names(der):
        if _looks_like_serial(name):
            return name.upper()
    return ""


def discover_printers(timeout: float = 10.0) -> list[dict]:
    """Discover Bambu printers on the LAN via SSDP + MQTT certificate CN."""
    found = _ssdp_scan(timeout=timeout)

    # 同一台机器可能先后以「有序列号 / 无序列号」两种键出现，按 IP 合并。
    merged: dict[str, dict] = {}
    for entry in found.values():
        current = merged.get(entry["ip"])
        if current is None:
            merged[entry["ip"]] = dict(entry)
            continue
        for field in ("serial", "model", "name"):
            if not current.get(field) and entry.get(field):
                current[field] = entry[field]
        if entry.get("signal") is not None:
            current["signal"] = entry["signal"]
    found = {entry["serial"] or entry["ip"]: entry for entry in merged.values()}

    missing = [entry["ip"] for entry in found.values() if not entry["serial"]]
    if missing:
        workers = min(16, len(missing))
        with ThreadPoolExecutor(max_workers=workers) as pool:
            serials = list(pool.map(_tls_serial, missing))
        by_ip = {entry["ip"]: entry for entry in found.values()}
        for ip, serial in zip(missing, serials):
            if not serial:
                continue
            entry = by_ip.get(ip)
            if entry is None:
                continue
            if entry["serial"] and entry["serial"] != serial:
                continue
            old_key = entry["serial"] or entry["ip"]
            entry["serial"] = serial
            entry["source"] = "cert"
            found.pop(old_key, None)
            found[serial] = entry

    devices = list(found.values())
    for entry in devices:
        entry["model"] = entry["model"] or ""
    devices.sort(key=lambda item: tuple(int(part) for part in item["ip"].split(".")))
    return devices


def _write_config_atomic(path: str, payload: dict) -> None:
    target = Path(path)
    tmp = target.with_name(target.name + ".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2) + "\n", "utf-8")
    os.chmod(tmp, 0o600)
    os.replace(tmp, target)


class EventHub:
    """Fan-out of SSE frames to connected dashboards."""

    def __init__(self) -> None:
        self._clients: set[queue.Queue] = set()
        self._lock = threading.Lock()

    def subscribe(self) -> queue.Queue:
        client: queue.Queue = queue.Queue(maxsize=200)
        with self._lock:
            self._clients.add(client)
        return client

    def unsubscribe(self, client: queue.Queue) -> None:
        with self._lock:
            self._clients.discard(client)

    def publish(self, event: str, payload: dict) -> None:
        frame = (
            f"event: {event}\n"
            f"data: {json.dumps(payload, ensure_ascii=False, separators=(',', ':'))}\n\n"
        )
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.put_nowait(frame)
            except queue.Full:
                # Slow tab: throw away the oldest frame, keep the newest.
                try:
                    client.get_nowait()
                    client.put_nowait(frame)
                except (queue.Empty, queue.Full):
                    pass


class UsageTracker:
    """Cumulative working hours per printer.

    Bambu firmware keeps a lifetime counter on the printer screen but does not
    publish it over MQTT, so we measure it locally while a job is active and
    persist the result in ``usage.json``.  ``baseline_hours`` in config.json
    lets the user add the counter already shown on the printer.
    """

    SAMPLE_INTERVAL = 10.0
    SAVE_INTERVAL = 30.0

    def __init__(self, path: Path | None) -> None:
        self.path = path
        self._lock = threading.Lock()
        self._seconds: dict[str, float] = {}
        self._last: dict[str, tuple[float, bool]] = {}
        self._printers: list["Printer"] = []
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._dirty = False
        self._load()

    def _load(self) -> None:
        if self.path is None or not self.path.is_file():
            return
        try:
            payload = json.loads(self.path.read_text("utf-8"))
            entries = payload.get("printers") if isinstance(payload, dict) else None
            if not isinstance(entries, dict):
                return
            for serial, item in entries.items():
                raw = item.get("seconds") if isinstance(item, dict) else item
                value = _as_float(raw)
                if value is not None and value >= 0:
                    self._seconds[str(serial)] = float(value)
            LOG.info("已载入累计工时: %d 台", len(self._seconds))
        except (OSError, ValueError) as exc:
            LOG.warning("累计工时文件读取失败，将从 0 重新统计: %s", exc)

    def save(self) -> None:
        if self.path is None:
            return
        with self._lock:
            if not self._dirty:
                return
            payload = {
                "version": 1,
                "printers": {
                    serial: {"seconds": round(seconds, 1)}
                    for serial, seconds in sorted(self._seconds.items())
                },
            }
            self._dirty = False
        tmp = self.path.with_name(self.path.name + ".tmp")
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), "utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, self.path)
        except OSError as exc:
            LOG.warning("累计工时保存失败: %s", exc)
            with self._lock:
                self._dirty = True

    def attach(self, printers: list["Printer"]) -> None:
        self._printers = list(printers)
        now = time.time()
        for printer in self._printers:
            self._last[printer.serial] = (now, printer.is_working())
        self._thread = threading.Thread(target=self._loop, name="usage-tracker", daemon=True)
        self._thread.start()

    def track(self, printer: "Printer") -> None:
        """Start accounting a printer added at runtime."""
        with self._lock:
            self._printers.append(printer)
            self._last[printer.serial] = (time.time(), printer.is_working())

    def untrack(self, serial: str) -> None:
        """Stop accounting a printer removed at runtime (history is kept)."""
        with self._lock:
            self._printers = [printer for printer in self._printers if printer.serial != serial]
            self._last.pop(serial, None)

    def _loop(self) -> None:
        last_save = time.monotonic()
        while not self._stop.wait(self.SAMPLE_INTERVAL):
            self._sample()
            if time.monotonic() - last_save >= self.SAVE_INTERVAL:
                self.save()
                last_save = time.monotonic()
        self.save()

    def _sample(self) -> None:
        now = time.time()
        # 先取各机状态，避免在持有工时锁时再去拿打印机锁。
        statuses = [(printer.serial, printer.is_working()) for printer in self._printers]
        with self._lock:
            for serial, active in statuses:
                previous = self._last.get(serial)
                if previous and previous[1]:
                    delta = now - previous[0]
                    # 服务刚启动或长时间卡顿时不补算，避免把停机时间算进去。
                    if 0 < delta <= self.SAMPLE_INTERVAL * 3:
                        self._seconds[serial] = self._seconds.get(serial, 0.0) + delta
                        self._dirty = True
                self._last[serial] = (now, active)

    def stop(self) -> None:
        self._sample()
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=3)
        self.save()

    def seconds_for(self, serial: str) -> float:
        with self._lock:
            return self._seconds.get(serial, 0.0)


class Printer:
    """Keeps the latest MQTT state of one printer and broadcasts changes."""

    def __init__(
        self,
        cfg: dict,
        hub: EventHub,
        index: int,
        mock: bool = False,
        usage: UsageTracker | None = None,
    ) -> None:
        self.serial = str(cfg["serial"]).strip()
        self.name = str(cfg.get("name") or self.serial[-6:]).strip()
        self.ip = str(cfg.get("ip") or "").strip()
        self.access_code = str(cfg.get("access_code") or "").strip()
        self.model = str(cfg.get("model") or "").strip()
        self.index = index
        self.hub = hub
        self.mock = mock
        self.usage = usage
        self.baseline_hours = max(0.0, _as_float(cfg.get("baseline_hours")) or 0.0)

        self.lock = threading.Lock()
        self.data = dict(DEFAULT_STATE)
        self.connected = False
        self.last_seen: float | None = None
        self.last_error = ""

        self.client = None
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._refresh_thread: threading.Thread | None = None
        self._seq = count(1)
        self._client_suffix = uuid.uuid4().hex[:6]

    # ------------------------------------------------------------------ state
    def public(self) -> dict:
        with self.lock:
            data = dict(self.data)
            connected = self.connected
            last_seen = self.last_seen

        state = str(data.get("gcode_state") or "IDLE").upper()
        if not connected:
            status = "OFFLINE"
        else:
            status = state if state in STATUS_LABELS else state or "IDLE"

        now = time.time()
        age = int(now - last_seen) if last_seen else None
        progress = _as_int(data.get("mc_percent")) or 0
        progress = max(0, min(100, progress))

        file_name = os.path.basename(str(data.get("gcode_file") or ""))
        subtask = str(data.get("subtask_name") or "")
        if not file_name:
            file_name = subtask

        tracked_seconds = self.usage.seconds_for(self.serial) if self.usage else 0.0
        usage_hours = self.baseline_hours + tracked_seconds / 3600.0

        return {
            "id": self.serial,
            "name": self.name,
            "model": self.model,
            "ip": self.ip,
            "index": self.index,
            "online": connected,
            "stale": age is None or age > 180,
            "status": status,
            "status_label": STATUS_LABELS.get(status, status),
            "progress": progress,
            "remaining_min": _as_int(data.get("mc_remaining_time")) or 0,
            "layer": _as_int(data.get("layer_num")) or 0,
            "total_layers": _as_int(data.get("total_layer_num")) or 0,
            "file": file_name,
            "subtask_name": subtask,
            "temps": {
                "nozzle": _as_float(data.get("nozzle_temper")),
                "nozzle_target": _as_float(data.get("nozzle_target_temper")),
                "bed": _as_float(data.get("bed_temper")),
                "bed_target": _as_float(data.get("bed_target_temper")),
                "chamber": _as_float(data.get("chamber_temper")),
            },
            "fans": {
                # Raw values are 0-15 (0 = off, 15 = full speed). The frontend
                # converts them to percentages.
                "cooling": _as_int(data.get("cooling_fan_speed")),
                "heatbreak": _as_int(data.get("heatbreak_fan_speed")),
                "aux": _first_int(data.get("big_fan1_speed"), data.get("aux_fan_speed")),
                "chamber": _first_int(data.get("big_fan2_speed"), data.get("chamber_fan_speed")),
            },
            "wifi": _parse_dbm(data.get("wifi_signal")),
            "error": _as_int(data.get("print_error")) or 0,
            "speed_level": _as_int(data.get("spd_lvl")),
            "speed_label": SPEED_LABELS.get(_as_int(data.get("spd_lvl")), ""),
            "usage_hours": round(usage_hours, 2),
            "usage_tracked_hours": round(tracked_seconds / 3600.0, 2),
            "usage_baseline_hours": round(self.baseline_hours, 2),
            "usage_active": self.is_working(),
            "last_seen": age,
            "last_error": self.last_error,
        }

    def is_working(self) -> bool:
        """True while the machine is preparing, printing or paused."""
        with self.lock:
            state = str(self.data.get("gcode_state") or "").upper()
            return self.connected and state in WORKING_STATES

    def _publish_state(self) -> None:
        self.hub.publish("update", {"type": "update", "printer": self.public(), "server_time": _now_ms()})

    def _set_connected(self, connected: bool) -> None:
        if self.connected == connected:
            return
        self.connected = connected
        self._publish_state()

    def _set_error(self, message: str) -> None:
        self.last_error = message
        LOG.warning("%s: %s", self.name, message)

    def _apply_print(self, payload: dict) -> None:
        with self.lock:
            for key in self.data:
                if key in payload and payload[key] is not None:
                    self.data[key] = payload[key]
            self.connected = True
            self.last_seen = time.time()
            self.last_error = ""
        self._publish_state()

    # ------------------------------------------------------------------- mqtt
    def start(self) -> None:
        target = self._mock_loop if self.mock else self._mqtt_loop
        self._thread = threading.Thread(target=target, name=f"printer-{self.name}", daemon=True)
        self._thread.start()
        if not self.mock:
            self._refresh_thread = threading.Thread(
                target=self._refresh_loop, name=f"refresh-{self.name}", daemon=True
            )
            self._refresh_thread.start()

    def stop(self) -> None:
        self._stop.set()
        client = self.client
        if client is not None:
            try:
                client.disconnect()
            except Exception:
                pass

    def _build_client(self):
        client = mqtt.Client(
            callback_api_version=mqtt.CallbackAPIVersion.VERSION2,
            client_id=f"bambu-mon-{self.serial[-8:]}-{self._client_suffix}",
            protocol=mqtt.MQTTv311,
            clean_session=True,
        )
        client.username_pw_set("bblp", self.access_code)
        # Bambu uses self-signed certificates. Some newer firmware (e.g. P2S)
        # does not answer a TLS 1.3 ClientHello, so cap the connection at 1.2.
        context = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
        context.minimum_version = ssl.TLSVersion.TLSv1_2
        context.maximum_version = ssl.TLSVersion.TLSv1_2
        client.tls_set_context(context)
        client.reconnect_delay_set(min_delay=3, max_delay=60)
        client.on_connect = self._on_connect
        client.on_disconnect = self._on_disconnect
        client.on_message = self._on_message
        return client

    def _on_connect(self, client, userdata, flags, reason_code, properties=None):
        failed = getattr(reason_code, "is_failure", None)
        if failed is None:
            failed = reason_code != 0
        if failed:
            self._set_error(f"MQTT 连接被拒绝 ({reason_code})，请检查访问码")
            return
        LOG.info("%s: MQTT 已连接", self.name)
        self.last_error = ""
        self._set_connected(True)
        client.subscribe(REPORT_TOPIC.format(serial=self.serial), qos=0)
        self._request_pushall()

    def _on_disconnect(self, *args):
        was_connected = self.connected
        self._set_connected(False)
        if was_connected and not self._stop.is_set():
            LOG.info("%s: MQTT 连接断开，正在自动重连", self.name)

    def _on_message(self, client, userdata, message):
        try:
            payload = json.loads(message.payload.decode("utf-8", errors="replace"))
        except (ValueError, UnicodeDecodeError):
            return
        report = payload.get("print")
        if isinstance(report, dict):
            self._apply_print(report)

    def _request_pushall(self) -> None:
        if self.client is None:
            return
        payload = {"pushing": {"sequence_id": str(next(self._seq)), "command": "pushall"}}
        self.client.publish(
            REQUEST_TOPIC.format(serial=self.serial),
            json.dumps(payload, separators=(",", ":")),
            qos=0,
        )

    def _mqtt_loop(self) -> None:
        if mqtt is None:
            self._set_error("缺少 paho-mqtt，请先执行 pip install -r requirements.txt")
            return
        delay = 3
        while not self._stop.is_set():
            try:
                self.client = self._build_client()
                self.client.connect(self.ip, MQTT_PORT, keepalive=30)
                delay = 3
                self.client.loop_forever(retry_first_connection=False)
            except Exception as exc:  # noqa: BLE001 - keep monitor alive
                if self._stop.is_set():
                    break
                self._set_error(f"连接 {self.ip}:{MQTT_PORT} 失败: {exc}")
                self._set_connected(False)
            finally:
                if self.client is not None:
                    try:
                        self.client.disconnect()
                    except Exception:
                        pass
            if self._stop.wait(delay):
                break
            delay = min(delay * 2, 30)

    def _refresh_loop(self) -> None:
        # Full state refresh; also keeps idle printers' data warm.
        while not self._stop.wait(45):
            if self.connected:
                try:
                    self._request_pushall()
                except Exception:
                    pass

    def send_command(self, action: str) -> tuple[bool, str]:
        if action not in ("pause", "resume", "stop"):
            return False, "不支持的操作"
        if not self.connected or self.client is None:
            return False, "打印机未连接"
        payload = {
            "print": {
                "sequence_id": str(next(self._seq)),
                "command": action,
            }
        }
        try:
            info = self.client.publish(
                REQUEST_TOPIC.format(serial=self.serial),
                json.dumps(payload, separators=(",", ":")),
                qos=0,
            )
        except Exception as exc:  # noqa: BLE001
            return False, f"发送失败: {exc}"
        if info.rc != mqtt.MQTT_ERR_SUCCESS:
            return False, f"发送失败 (MQTT {info.rc})"
        return True, "指令已发送"

    # ------------------------------------------------------------------- mock
    def _mock_loop(self) -> None:
        rng = random.Random(9000 + self.index)
        while not self._stop.wait(1.4 + rng.random() * 1.4):
            if not self.connected:
                continue
            with self.lock:
                data = self.data
                state = str(data["gcode_state"])
                if state == "RUNNING":
                    data["mc_percent"] = min(100.0, float(data["mc_percent"]) + rng.uniform(0.06, 0.45))
                    if data["mc_percent"] >= 100:
                        data["gcode_state"] = "FINISH"
                        data["mc_percent"] = 100
                        data["layer_num"] = data["total_layer_num"]
                        data["mc_remaining_time"] = 0
                        self._mock_targets(0, 0, 0, 0)
                        data["nozzle_target_temper"] = 0
                        data["bed_target_temper"] = 0
                progress = float(data["mc_percent"])
                total_min = self.mock_total_min
                if data["gcode_state"] in ("RUNNING", "PAUSE"):
                    data["mc_remaining_time"] = int(total_min * (100 - progress) / 100)
                    data["layer_num"] = int(progress / 100 * data["total_layer_num"])
                else:
                    data["mc_remaining_time"] = 0
                    if data["gcode_state"] == "IDLE":
                        data["layer_num"] = 0
                    elif data["gcode_state"] == "FAILED":
                        data["layer_num"] = int(progress / 100 * data["total_layer_num"])

                for field, base in self.mock_fan_base.items():
                    if data["gcode_state"] == "RUNNING":
                        value = max(0, min(15, base + rng.randint(-1, 1)))
                    elif data["gcode_state"] == "PAUSE":
                        value = base // 2
                    else:
                        value = 0
                    data[field] = value

                nozzle_target = float(data["nozzle_target_temper"])
                nozzle_goal = nozzle_target if nozzle_target > 0 else 24
                data["nozzle_temper"] = self._ease(data["nozzle_temper"], nozzle_goal, 2.5)
                bed_target = float(data["bed_target_temper"])
                bed_goal = bed_target if bed_target > 0 else 24
                data["bed_temper"] = self._ease(data["bed_temper"], bed_goal, 1.2)
                if data["chamber_temper"] is not None:
                    chamber_target = 38 if data["gcode_state"] == "RUNNING" else 24
                    data["chamber_temper"] = self._ease(data["chamber_temper"], chamber_target, 0.4)
                data["wifi_signal"] = self.mock_wifi + rng.randint(-3, 3)
                if data["gcode_state"] == "RUNNING" and rng.random() < 0.0002:
                    data["gcode_state"] = "PAUSE"
                self.last_seen = time.time()
            self._publish_state()

    @staticmethod
    def _ease(current, target, step):
        current = float(current or 0)
        target = float(target or 0)
        if abs(target - current) <= step:
            return round(target, 1)
        return round(current + step * (1 if target > current else -1), 1)

    def _mock_targets(self, cooling=0, heatbreak=0, aux=0, chamber=0):
        self.mock_fan_base = {
            "cooling_fan_speed": cooling,
            "heatbreak_fan_speed": heatbreak,
            "aux_fan_speed": aux,
            "chamber_fan_speed": chamber,
        }

    def mock_command(self, action: str) -> tuple[bool, str]:
        with self.lock:
            state = str(self.data["gcode_state"])
            if action == "pause":
                if state != "RUNNING":
                    return False, "当前没有正在打印的任务"
                self.data["gcode_state"] = "PAUSE"
            elif action == "resume":
                if state != "PAUSE":
                    return False, "当前任务不在暂停状态"
                self.data["gcode_state"] = "RUNNING"
            elif action == "stop":
                if state not in ("RUNNING", "PAUSE", "PREPARE"):
                    return False, "当前没有可停止的任务"
                self.data["gcode_state"] = "IDLE"
                self.data["mc_percent"] = 0
                self.data["mc_remaining_time"] = 0
                self.data["layer_num"] = 0
                self.data["nozzle_target_temper"] = 0
                self.data["bed_target_temper"] = 0
            else:
                return False, "不支持的操作"
            self.last_seen = time.time()
        self._publish_state()
        return True, "指令已发送"


class MockPrinter(Printer):
    """Synthetic printer used by --mock so the dashboard can be previewed."""

    FILES = [
        ("Bambu_Benchy.3mf", "3D_Benchy"),
        ("Phone_Stand.3mf", "Phone_Stand_v2"),
        ("Cable_Clip_Set.3mf", "Cable_Clip_x8"),
        ("Spiral_Vase.3mf", "Spiral_Vase"),
        ("Gear_Box.3mf", "Planetary_Gears"),
        ("Desk_Organizer.3mf", "Desk_Organizer"),
        ("Miniature_Knight.3mf", "Knight_32mm"),
        ("Tool_Holder.3mf", "Tool_Holder"),
    ]

    STATUSES = (
        ["RUNNING"] * 12
        + ["PAUSE"] * 2
        + ["FINISH"] * 3
        + ["IDLE"] * 2
        + ["FAILED"] * 1
    )

    def __init__(self, index: int, hub: EventHub, usage: UsageTracker | None = None) -> None:
        rng = random.Random(100 + index)
        models = ["X1 Carbon"] * 5 + ["P1S"] * 5 + ["A1"] * 5 + ["A1 mini"] * 5
        prefixes = {"X1 Carbon": "X1C", "P1S": "P1S", "A1": "A1", "A1 mini": "A1mini"}
        model = models[index % len(models)]
        name = f"{prefixes[model]}-{index % 5 + 1:02d}"
        cfg = {
            "name": name,
            "model": model,
            "serial": f"MOCK{index:012d}",
            "ip": f"192.168.1.{100 + index}",
            "access_code": "00000000",
            # 模拟模式给一个像样的历史工时，便于预览显示效果。
            "baseline_hours": round(rng.uniform(20, 900), 1),
        }
        super().__init__(cfg, hub, index, mock=True, usage=usage)
        self.mock_total_min = rng.randint(35, 420)
        self.mock_wifi = rng.randint(-68, -42)
        self.mock_offline = index == 18

        self.data["task_id"] = f"mock-{index}"
        self.data["print_error"] = 0
        self.data["total_layer_num"] = rng.randint(80, 340)
        self.data["cooling_fan_speed"] = 0
        self.data["heatbreak_fan_speed"] = 0
        self.data["aux_fan_speed"] = None if model == "A1 mini" else 0
        self.data["chamber_fan_speed"] = 0 if model in ("X1 Carbon", "P1S") else None
        self.data["chamber_temper"] = 24.0 if model in ("X1 Carbon", "P1S") else None

        status = self.STATUSES[index]
        self.data["gcode_state"] = status
        self.data["spd_lvl"] = rng.choice([1, 2, 2, 3, 4])
        file_name, subtask = self.FILES[index % len(self.FILES)]
        self.data["gcode_file"] = file_name
        self.data["subtask_name"] = subtask
        self.data["nozzle_temper"] = 24.0
        self.data["bed_temper"] = 24.0
        self.data["nozzle_target_temper"] = 0
        self.data["bed_target_temper"] = 0
        self.data["wifi_signal"] = self.mock_wifi
        self._mock_targets(
            rng.randint(8, 15),
            15,
            rng.randint(4, 11) if model != "A1 mini" else 0,
            rng.randint(3, 9) if model in ("X1 Carbon", "P1S") else 0,
        )

        if status in ("RUNNING", "PAUSE", "FAILED"):
            self.data["mc_percent"] = rng.randint(4, 92)
            self.data["nozzle_target_temper"] = rng.choice([210, 220, 230, 240])
            self.data["bed_target_temper"] = rng.choice([55, 60, 65])
            self.data["nozzle_temper"] = self.data["nozzle_target_temper"] - rng.randint(0, 8)
            self.data["bed_temper"] = self.data["bed_target_temper"] - rng.randint(0, 3)
            if status == "FAILED":
                self.data["print_error"] = 83935248
        elif status == "FINISH":
            self.data["mc_percent"] = 100
            self.data["layer_num"] = self.data["total_layer_num"]
            self.data["nozzle_temper"] = rng.randint(28, 40)
            self.data["bed_temper"] = rng.randint(25, 32)

        if self.mock_offline:
            self.connected = False
            self.last_seen = time.time() - 3400
            self.data["gcode_state"] = "RUNNING"
            self.data["mc_percent"] = 62
            self.data["nozzle_temper"] = 220
            self.data["nozzle_target_temper"] = 220
            self.data["bed_temper"] = 60
            self.data["bed_target_temper"] = 60
            self.data["layer_num"] = 84
            self.data["total_layer_num"] = 136
            self.data["mc_remaining_time"] = 52
            self.data["cooling_fan_speed"] = 11
            self.data["heatbreak_fan_speed"] = 15
            if self.data["aux_fan_speed"] is not None:
                self.data["aux_fan_speed"] = 7
            if self.data["chamber_fan_speed"] is not None:
                self.data["chamber_fan_speed"] = 5
        else:
            self.connected = True
            self.last_seen = time.time() - rng.randint(0, 3)

    def start(self) -> None:
        self._thread = threading.Thread(target=self._mock_loop, name=f"mock-{self.name}", daemon=True)
        self._thread.start()

    def send_command(self, action: str) -> tuple[bool, str]:
        if not self.connected:
            return False, "打印机离线"
        return self.mock_command(action)


class App:
    def __init__(
        self,
        config: dict,
        mock: bool = False,
        mock_count: int = 20,
        config_path: str | None = None,
    ) -> None:
        self.config = config
        self.hub = EventHub()
        self.title = str(config.get("title") or "打印工坊")
        server_cfg = config.get("server") or {}
        self.allow_control = bool(server_cfg.get("allow_control", False))
        self.allow_manage = bool(server_cfg.get("allow_manage", True))
        self.printers: list[Printer] = []
        self._by_id: dict[str, Printer] = {}
        self.config_path = config_path
        self.mock = mock
        self._config_lock = threading.Lock()

        usage_path = None
        if not mock:
            env_usage = os.environ.get("BAMBU_USAGE_FILE")
            if env_usage:
                usage_path = Path(env_usage).expanduser()
            else:
                base = Path(config_path).resolve().parent if config_path else BASE_DIR
                usage_path = base / "usage.json"
        self.usage = UsageTracker(usage_path)

        if mock:
            self.allow_control = True
            self.printers = [MockPrinter(i, self.hub, self.usage) for i in range(mock_count)]
            for printer in self.printers:
                self._by_id[printer.serial] = printer
            return

        for index, item in enumerate(config.get("printers") or []):
            if not isinstance(item, dict):
                LOG.warning("忽略第 %d 个打印机配置：格式不正确", index + 1)
                continue
            serial = str(item.get("serial") or "").strip()
            ip = str(item.get("ip") or "").strip()
            code = str(item.get("access_code") or "").strip()
            if not (serial and ip and code):
                LOG.warning("忽略第 %d 个打印机配置：serial / ip / access_code 不完整", index + 1)
                continue
            if serial in self._by_id:
                LOG.warning("忽略重复序列号: %s", serial)
                continue
            printer = Printer(item, self.hub, index, usage=self.usage)
            self.printers.append(printer)
            self._by_id[serial] = printer

        if not self.printers:
            LOG.warning("配置中没有有效的打印机，页面将显示为空。")

    def start(self) -> None:
        for printer in self.printers:
            printer.start()
        self.usage.attach(self.printers)

    def stop(self) -> None:
        self.usage.stop()
        for printer in self.printers:
            printer.stop()

    def find(self, serial: str) -> Printer | None:
        return self._by_id.get(serial)

    # ----------------------------------------------------------- manage (web)
    def _load_config_for_write(self) -> dict:
        if self.config_path:
            try:
                return load_config(self.config_path)
            except (OSError, ValueError) as exc:
                LOG.warning("读取配置文件失败，使用内存中的配置: %s", exc)
        return json.loads(json.dumps(self.config))

    def add_printer(self, payload: dict) -> tuple[bool, str]:
        if self.mock:
            return False, "模拟模式下不能添加打印机"
        if not self.config_path:
            return False, "未指定配置文件，无法保存"

        serial = str(payload.get("serial") or "").strip().upper()
        ip = _valid_ipv4(payload.get("ip"))
        access_code = str(payload.get("access_code") or "").strip()
        name = str(payload.get("name") or "").strip() or (serial[-6:] if serial else "")
        model = str(payload.get("model") or "").strip()
        baseline = _as_float(payload.get("baseline_hours")) or 0.0

        if not _looks_like_serial(serial):
            return False, "序列号格式不正确（应为 15~18 位字母数字）"
        if not ip:
            return False, "IP 地址格式不正确"
        if not access_code:
            return False, "请填写访问码"
        if len(access_code) > 64:
            return False, "访问码过长"
        baseline = max(0.0, baseline)

        with self._config_lock:
            if serial in self._by_id:
                return False, "该序列号已在监控列表中"
            if any(printer.ip == ip for printer in self.printers):
                return False, f"IP {ip} 已在监控列表中"

            data = self._load_config_for_write()
            entries = data.get("printers")
            if not isinstance(entries, list):
                entries = []
            if any(
                str(item.get("serial") or "").strip().upper() == serial
                for item in entries
                if isinstance(item, dict)
            ):
                return False, "该序列号已在配置文件中"

            record = {
                "name": name,
                "model": model,
                "serial": serial,
                "ip": ip,
                "access_code": access_code,
                "baseline_hours": round(baseline, 1),
            }
            entries.append(record)
            data["printers"] = entries
            try:
                _write_config_atomic(self.config_path, data)
            except OSError as exc:
                return False, f"写入配置文件失败: {exc}"
            self.config = data

            index = max((printer.index for printer in self.printers), default=-1) + 1
            printer = Printer(record, self.hub, index, usage=self.usage)
            self.printers.append(printer)
            self._by_id[serial] = printer
            self.usage.track(printer)
            printer.start()

        LOG.info("已通过网页添加打印机 %s (%s)", name, ip)
        self.hub.publish(
            "update",
            {"type": "update", "printer": printer.public(), "server_time": _now_ms()},
        )
        return True, f"已添加「{name}」，正在连接打印机"

    def remove_printer(self, serial: str) -> tuple[bool, str]:
        if self.mock:
            return False, "模拟模式下不能移除打印机"
        if not self.config_path:
            return False, "未指定配置文件，无法保存"

        serial = str(serial or "").strip().upper()
        printer = self._by_id.get(serial)
        if printer is None:
            return False, "找不到该打印机"

        with self._config_lock:
            data = self._load_config_for_write()
            entries = data.get("printers")
            if not isinstance(entries, list):
                entries = []
            kept = [
                item
                for item in entries
                if not (isinstance(item, dict) and str(item.get("serial") or "").strip().upper() == serial)
            ]
            data["printers"] = kept
            try:
                _write_config_atomic(self.config_path, data)
            except OSError as exc:
                return False, f"写入配置文件失败: {exc}"
            self.config = data

            self.printers = [item for item in self.printers if item.serial != serial]
            self._by_id.pop(serial, None)
            self.usage.untrack(serial)
            printer.stop()

        LOG.info("已通过网页移除打印机 %s", printer.name)
        self.hub.publish("remove", {"type": "remove", "id": serial})
        return True, f"已移除「{printer.name}」"

    def snapshot(self) -> dict:
        return {
            "type": "snapshot",
            "config": {
                "title": self.title,
                "allow_control": self.allow_control,
                "allow_manage": self.allow_manage,
                "count": len(self.printers),
            },
            "printers": [printer.public() for printer in self.printers],
            "server_time": _now_ms(),
        }


class MonitorServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, handler, app: App) -> None:
        # host 为 "::" 时使用 IPv6 套接字；Linux 默认 bindv6only=0，
        # 可同时接受 IPv6 和 IPv4（映射地址）连接。
        self.address_family = socket.AF_INET6 if ":" in str(address[0]) else socket.AF_INET
        super().__init__(address, handler)
        self.app = app


class Handler(BaseHTTPRequestHandler):
    server_version = "BambuMonitor/1.0"
    protocol_version = "HTTP/1.1"

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    def log_message(self, fmt, *args):
        LOG.debug("%s - %s", self.address_string(), fmt % args)

    # ------------------------------------------------------------------- HTTP
    def do_GET(self):  # noqa: N802 - http.server API
        path = urlparse(self.path).path
        if path == "/api/events":
            self._serve_events()
        elif path == "/api/printers":
            self._send_json({"printers": [p.public() for p in self.app.printers], "server_time": _now_ms()})
        elif path == "/api/config":
            self._send_json(
                {
                    "title": self.app.title,
                    "allow_control": self.app.allow_control,
                    "allow_manage": self.app.allow_manage,
                    "count": len(self.app.printers),
                }
            )
        elif path == "/healthz":
            self._send_json({"ok": True, "printers": len(self.app.printers)})
        elif path in ("/", "/index.html"):
            self._serve_file(STATIC_DIR / "index.html")
        elif path.startswith("/static/"):
            rel = unquote(path[len("/static/"):])
            self._serve_file((STATIC_DIR / rel).resolve())
        else:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)

    def do_POST(self):  # noqa: N802 - http.server API
        path = urlparse(self.path).path

        if path == "/api/scan":
            self._handle_scan()
            return
        if path == "/api/printers":
            self._handle_add_printer()
            return

        match = re.fullmatch(r"/api/printers/([^/]+)/command", path)
        if not match:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if not self.app.allow_control:
            self._send_json(
                {"error": "远程控制已禁用，请在 config.json 中将 server.allow_control 设为 true"},
                HTTPStatus.FORBIDDEN,
            )
            return
        body = self._read_json()
        if not isinstance(body, dict):
            self._send_json({"error": "JSON 解析失败"}, HTTPStatus.BAD_REQUEST)
            return
        action = str(body.get("action") or "")
        printer = self.app.find(unquote(match.group(1)))
        if printer is None:
            self._send_json({"error": "找不到该打印机"}, HTTPStatus.NOT_FOUND)
            return
        ok, message = printer.send_command(action)
        if not ok:
            self._send_json({"error": message}, HTTPStatus.CONFLICT)
            return
        self._send_json({"ok": True, "message": message})

    def do_DELETE(self):  # noqa: N802 - http.server API
        path = urlparse(self.path).path
        match = re.fullmatch(r"/api/printers/([^/]+)", path)
        if not match:
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if not self.app.allow_manage:
            self._send_json(
                {"error": "网页管理已禁用，请在 config.json 中将 server.allow_manage 设为 true"},
                HTTPStatus.FORBIDDEN,
            )
            return
        serial = unquote(match.group(1)).strip()
        ok, message = self.app.remove_printer(serial)
        if not ok:
            status = HTTPStatus.NOT_FOUND if "找不到" in message else HTTPStatus.BAD_REQUEST
            self._send_json({"error": message}, status)
            return
        self._send_json({"ok": True, "message": message})

    # ------------------------------------------------------------- management
    def _handle_scan(self) -> None:
        if not self.app.allow_manage:
            self._send_json(
                {"error": "网页管理已禁用，请在 config.json 中将 server.allow_manage 设为 true"},
                HTTPStatus.FORBIDDEN,
            )
            return
        started = time.monotonic()
        try:
            devices = discover_printers()
        except Exception as exc:  # noqa: BLE001 - never take the dashboard down
            LOG.exception("局域网扫描失败")
            self._send_json({"error": f"扫描失败: {exc}"}, HTTPStatus.INTERNAL_SERVER_ERROR)
            return

        known_serial = set(self.app._by_id)
        known_ip = {printer.ip for printer in self.app.printers}
        for device in devices:
            device["known"] = bool(
                (device.get("serial") and device["serial"] in known_serial)
                or (device.get("ip") and device["ip"] in known_ip)
            )
        LOG.info("局域网扫描完成：发现 %d 台打印机（%.1fs）", len(devices), time.monotonic() - started)
        self._send_json(
            {
                "devices": devices,
                "count": len(devices),
                "scanned_in": round(time.monotonic() - started, 2),
            }
        )

    def _handle_add_printer(self) -> None:
        if not self.app.allow_manage:
            self._send_json(
                {"error": "网页管理已禁用，请在 config.json 中将 server.allow_manage 设为 true"},
                HTTPStatus.FORBIDDEN,
            )
            return
        content_type = (self.headers.get("Content-Type") or "").split(";", 1)[0].strip().lower()
        # 只接受 JSON，避免第三方页面用简单请求跨站写入打印机配置。
        if content_type != "application/json":
            self._send_json({"error": "Content-Type 必须是 application/json"}, HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
            return
        body = self._read_json()
        if not isinstance(body, dict):
            self._send_json({"error": "请求体无效"}, HTTPStatus.BAD_REQUEST)
            return
        ok, message = self.app.add_printer(body)
        if not ok:
            status = HTTPStatus.CONFLICT if ("已在" in message) else HTTPStatus.BAD_REQUEST
            self._send_json({"error": message}, status)
            return
        self._send_json({"ok": True, "message": message})

    def _read_json(self, limit: int = 8192):
        try:
            length = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            length = 0
        if length <= 0 or length > limit:
            return None
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (ValueError, UnicodeDecodeError):
            return None

    # ---------------------------------------------------------------- helpers
    def _send_json(self, payload: dict, status: HTTPStatus = HTTPStatus.OK) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_file(self, path: Path) -> None:
        try:
            resolved = path.resolve()
            resolved.relative_to(STATIC_DIR.resolve())
        except (ValueError, OSError):
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        if not resolved.is_file():
            self._send_json({"error": "not found"}, HTTPStatus.NOT_FOUND)
            return
        content_type = mimetypes.guess_type(str(resolved))[0] or "application/octet-stream"
        if resolved.suffix in (".js", ".css", ".html", ".svg"):
            content_type += "; charset=utf-8"
        data = resolved.read_bytes()
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.end_headers()
        try:
            self.wfile.write(data)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _serve_events(self) -> None:
        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache, no-transform")
        self.send_header("Connection", "keep-alive")
        self.send_header("X-Accel-Buffering", "no")
        self.end_headers()

        client = self.app.hub.subscribe()
        try:
            self._write_frame(
                "snapshot",
                json.dumps(self.app.snapshot(), ensure_ascii=False, separators=(",", ":")),
            )
            while True:
                try:
                    frame = client.get(timeout=20)
                except queue.Empty:
                    self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
                    continue
                self.wfile.write(frame.encode("utf-8"))
                self.wfile.flush()
        except (BrokenPipeError, ConnectionResetError, OSError):
            pass
        finally:
            self.app.hub.unsubscribe(client)
            self.close_connection = True

    def _write_frame(self, event: str, data: str) -> None:
        self.wfile.write(f"event: {event}\ndata: {data}\n\n".encode("utf-8"))
        self.wfile.flush()


def load_config(path: str) -> dict:
    with open(path, "r", encoding="utf-8") as handle:
        return json.load(handle)


def local_ip() -> str:
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        sock.connect(("8.8.8.8", 80))
        return sock.getsockname()[0]
    except OSError:
        return "127.0.0.1"
    finally:
        sock.close()


def main() -> None:
    parser = argparse.ArgumentParser(description="拓竹打印机局域网监控")
    parser.add_argument("--config", default=str(BASE_DIR / "config.json"), help="配置文件路径")
    parser.add_argument("--host", help="监听地址（覆盖配置文件）")
    parser.add_argument("--port", type=int, help="监听端口（覆盖配置文件）")
    parser.add_argument("--mock", action="store_true", help="使用模拟打印机，便于预览界面")
    parser.add_argument("--mock-count", type=int, default=20, help="模拟打印机数量（默认 20）")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)-7s %(message)s",
        datefmt="%H:%M:%S",
    )

    config: dict = {}
    if args.mock:
        try:
            config = load_config(args.config)
        except (OSError, ValueError):
            config = {}
    else:
        try:
            config = load_config(args.config)
        except OSError:
            LOG.error("找不到配置文件 %s", args.config)
            LOG.error("请复制 config.example.json 为 config.json 并填写打印机信息。")
            raise SystemExit(1) from None
        except ValueError as exc:
            LOG.error("配置文件 JSON 格式错误: %s", exc)
            raise SystemExit(1) from None

    if not args.mock and mqtt is None:
        LOG.error("缺少 paho-mqtt 依赖，请执行: pip install -r requirements.txt")
        raise SystemExit(1)

    server_cfg = config.get("server") or {}
    host = args.host or server_cfg.get("host") or "0.0.0.0"
    port = args.port or int(server_cfg.get("port") or 8088)

    app = App(
        config,
        mock=args.mock,
        mock_count=max(1, min(args.mock_count, 100)),
        config_path=args.config,
    )
    app.start()

    httpd = MonitorServer((host, port), Handler, app)
    shown_host = local_ip() if host in ("0.0.0.0", "::") else host
    LOG.info("监控面板已启动: http://%s:%d", shown_host, port)
    LOG.info("共 %d 台打印机%s", len(app.printers), "（模拟模式）" if args.mock else "")
    if app.allow_control:
        LOG.warning("远程控制已开启（暂停 / 继续 / 停止）")

    def handle_stop(signum, _frame):
        # systemd 停止/重启时发送 SIGTERM，需要走 finally 保存累计工时。
        LOG.info("收到停止信号 (%s)，正在退出...", signum)
        raise SystemExit(0)

    signal.signal(signal.SIGTERM, handle_stop)
    signal.signal(signal.SIGINT, handle_stop)

    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        LOG.info("正在停止...")
    finally:
        app.stop()
        httpd.server_close()


if __name__ == "__main__":
    main()
