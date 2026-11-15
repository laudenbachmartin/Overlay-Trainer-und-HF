"""
Trainings-Overlay
=================
Startmaske mit Kacheln (Herzfrequenz, Leistung, Trittfrequenz). Ein Klick auf
eine Kachel sucht passende Bluetooth-LE-Geräte, danach führt "Weiter" zum
schwebenden, transparenten Overlay über allen Fenstern.

Start:  py overlay.py   (oder start_overlay.bat)

Overlay-Bedienung:
    Linke Maustaste auf den Zahlen ziehen -> verschieben
    Mausrad                               -> Größe
    Rechte Maustaste                      -> Menü (Hintergrund, Deckkraft, Geräte, Beenden)
"""

import asyncio
import collections
import json
import os
import queue
import struct
import sys
import threading
import time
import tkinter as tk
import tkinter.font as tkfont


def uuid16(short: int) -> str:
    return f"0000{short:04x}-0000-1000-8000-00805f9b34fb"


HR_SERVICE, HR_MEAS = uuid16(0x180D), uuid16(0x2A37)       # Herzfrequenz
CPS_SERVICE, CPS_MEAS = uuid16(0x1818), uuid16(0x2A63)     # Cycling Power
CSC_SERVICE, CSC_MEAS = uuid16(0x1816), uuid16(0x2A5B)     # Speed & Cadence
FTMS_SERVICE, FTMS_BIKE = uuid16(0x1826), uuid16(0x2AD2)   # Fitness Machine (Trainer)

ROLES = {
    "hr": {"title": "Herzfrequenz", "icon": "♥", "unit": "bpm",
           "services": [HR_SERVICE], "ant": [120]},
    "power": {"title": "Leistung", "icon": "⚡", "unit": "W",
              "services": [FTMS_SERVICE, CPS_SERVICE], "ant": [17, 11]},
    "cadence": {"title": "Trittfrequenz", "icon": "↻", "unit": "rpm",
                "services": [FTMS_SERVICE, CPS_SERVICE, CSC_SERVICE], "ant": [17, 11, 122, 121]},
}
STALE_SECONDS = 5


# ---------------------------------------------------------------------------
# Parser (reine Funktionen)
# ---------------------------------------------------------------------------

def parse_heart_rate(data: bytes) -> int:
    if data[0] & 0x01:
        return struct.unpack_from("<H", data, 1)[0]
    return data[1]


def parse_ftms_indoor_bike(data: bytes) -> dict:
    flags = struct.unpack_from("<H", data, 0)[0]
    pos, out = 2, {}
    if not flags & 0x0001:
        out["speed"] = struct.unpack_from("<H", data, pos)[0] / 100.0
        pos += 2
    if flags & 0x0002:
        pos += 2
    if flags & 0x0004:
        out["cadence"] = struct.unpack_from("<H", data, pos)[0] / 2.0
        pos += 2
    if flags & 0x0008:
        pos += 2
    if flags & 0x0010:
        pos += 3
    if flags & 0x0020:
        pos += 2
    if flags & 0x0040:
        out["power"] = struct.unpack_from("<h", data, pos)[0]
    return out


class CrankCadence:
    """Berechnet Kadenz aus kumulierten Kurbelumdrehungen (Zeitbasis 1/1024 s)."""

    def __init__(self):
        self.revs = self.t = None
        self.last_change = 0.0
        self.cadence = 0.0

    def update(self, revs: int, ev_time: int, now: float) -> float:
        if self.revs is not None:
            d_r = (revs - self.revs) & 0xFFFF
            d_t = (ev_time - self.t) & 0xFFFF
            if d_r and d_t:
                c = d_r * 60 * 1024 / d_t
                if c < 250:
                    self.cadence = c
                self.last_change = now
            elif now - self.last_change > 3:
                self.cadence = 0.0
        else:
            self.last_change = now
        self.revs, self.t = revs, ev_time
        return self.cadence


def parse_cycling_power(data: bytes, crank: CrankCadence, now: float) -> dict:
    flags = struct.unpack_from("<H", data, 0)[0]
    out = {"power": struct.unpack_from("<h", data, 2)[0]}
    pos = 4
    if flags & 0x0001:
        pos += 1
    if flags & 0x0004:
        pos += 2
    if flags & 0x0010:
        pos += 6
    if flags & 0x0020:
        revs, t = struct.unpack_from("<HH", data, pos)
        out["cadence"] = crank.update(revs, t, now)
    return out


def parse_csc(data: bytes, crank: CrankCadence, now: float) -> dict:
    flags, pos = data[0], 1
    if flags & 0x01:
        pos += 6
    if flags & 0x02:
        revs, t = struct.unpack_from("<HH", data, pos)
        return {"cadence": crank.update(revs, t, now)}
    return {}


# ---------------------------------------------------------------------------
# Einstellungen
# ---------------------------------------------------------------------------

def settings_path() -> str:
    base = os.path.dirname(sys.executable if getattr(sys, "frozen", False)
                           else os.path.abspath(__file__))
    return os.path.join(base, "settings.json")


DEFAULTS = {"devices": {}, "max_hr": 180, "hr_zones": None, "show_hr_zone": True, "min_rpm": 0, "layout": "vertical", "ftp": 200,
            "show_3s": True, "show_avg_power": True, "show_avg_hr": True,
            "alpha": 0.95, "scale": 1.0, "x": 40, "y": 40, "panel": False}


def load_settings() -> dict:
    s = json.loads(json.dumps(DEFAULTS))
    try:
        with open(settings_path(), encoding="utf-8") as f:
            raw = json.load(f)
        s.update({k: v for k, v in raw.items() if k in DEFAULTS and v is not None})
    except Exception:
        pass
    return s


def save_settings(s: dict):
    try:
        with open(settings_path(), "w", encoding="utf-8") as f:
            json.dump(s, f, indent=2, ensure_ascii=False)
    except Exception:
        pass


# ---------------------------------------------------------------------------
# Bluetooth-Manager (eigener Thread mit asyncio-Loop)
# ---------------------------------------------------------------------------

class BleManager:
    def __init__(self, out_q: queue.Queue):
        self.q = out_q
        self.loop = asyncio.new_event_loop()
        self.tasks = {}  # address -> (task, roles)
        threading.Thread(target=self._run, daemon=True).start()

    def _run(self):
        asyncio.set_event_loop(self.loop)
        self.loop.run_forever()

    # --- Aufrufe aus der Oberfläche ---
    def scan(self, timeout=5.0):
        return asyncio.run_coroutine_threadsafe(self._scan(timeout), self.loop)

    def apply(self, roles: dict):
        """roles: {role: {"address": ..., "name": ...}}"""
        mapping = {r: d["address"] for r, d in roles.items()}
        asyncio.run_coroutine_threadsafe(self._apply(mapping), self.loop)

    # --- intern ---
    async def _scan(self, timeout):
        from bleak import BleakScanner
        found = await BleakScanner.discover(timeout=timeout, return_adv=True)
        return [{"address": d.address,
                 "name": a.local_name or d.name or "",
                 "rssi": a.rssi,
                 "uuids": [u.lower() for u in a.service_uuids]}
                for d, a in found.values()]

    async def _apply(self, mapping):
        wanted = {}
        for role, addr in mapping.items():
            wanted.setdefault(addr, set()).add(role)
        stopped = []
        for addr, (task, roles) in list(self.tasks.items()):
            if wanted.get(addr) != roles:
                task.cancel()
                stopped.append(task)
                del self.tasks[addr]
        if stopped:
            await asyncio.gather(*stopped, return_exceptions=True)
        for addr, roles in wanted.items():
            if addr not in self.tasks:
                self.tasks[addr] = (asyncio.create_task(self._device(addr, roles)), roles)

    async def _device(self, address, roles):
        from bleak import BleakClient
        loop = asyncio.get_running_loop()

        def status(kind, msg):
            for r in roles:
                self.q.put(("status", r, address, kind, msg))

        def emit(values: dict):
            now = time.monotonic()
            for r in roles:
                if values.get(r) is not None:
                    self.q.put(("value", r, address, values[r], now))

        def safe(fn):
            def cb(_sender, data):
                try:
                    emit(fn(bytes(data)))
                except Exception:
                    pass
            return cb

        while True:
            status("connecting", "verbinde …")
            gone = asyncio.Event()
            try:
                async with BleakClient(
                    address, timeout=20,
                    disconnected_callback=lambda _c: loop.call_soon_threadsafe(gone.set),
                ) as client:
                    svc = client.services
                    has = lambda u: svc.get_characteristic(u) is not None  # noqa: E731
                    missing = []
                    if "hr" in roles:
                        if has(HR_MEAS):
                            await client.start_notify(
                                HR_MEAS, safe(lambda d: {"hr": parse_heart_rate(d)}))
                        else:
                            missing.append("HF")
                    if roles & {"power", "cadence"}:
                        if has(FTMS_BIKE):
                            await client.start_notify(FTMS_BIKE, safe(parse_ftms_indoor_bike))
                        elif has(CPS_MEAS):
                            crank = CrankCadence()
                            await client.start_notify(CPS_MEAS, safe(
                                lambda d: parse_cycling_power(d, crank, time.monotonic())))
                        elif has(CSC_MEAS):
                            crank = CrankCadence()
                            await client.start_notify(CSC_MEAS, safe(
                                lambda d: parse_csc(d, crank, time.monotonic())))
                            if "power" in roles:
                                missing.append("Leistung")
                        else:
                            missing.append("Leistung/Kadenz")
                    if missing:
                        status("error", "liefert keine " + ", ".join(missing))
                    else:
                        status("connected", "verbunden")
                    await gone.wait()
            except asyncio.CancelledError:
                raise
            except Exception as e:
                status("error", str(e)[:60] or type(e).__name__)
            status("connecting", "getrennt – neuer Versuch …")
            await asyncio.sleep(3)


# ---------------------------------------------------------------------------
# ANT+ (USB-Stick, eigener Thread)
# ---------------------------------------------------------------------------

ANT_NETWORK_KEY = (0xB9, 0xA5, 0x21, 0xFB, 0xBD, 0x72, 0xC3, 0x45)
ANT_FREQ = 57
ANT_STICKS = [(0x0FCF, 0x1009), (0x0FCF, 0x1008)]  # ANT USB-m, ANT USB2
ANT_TYPES = {  # Gerätetyp: (Bezeichnung, Kanalperiode)
    120: ("Herzfrequenz", 8070),
    11: ("Leistung", 8182),
    17: ("Trainer (FE-C)", 8192),
    121: ("Speed/Cadence", 8086),
    122: ("Kadenz", 8102),
}
ANT_MAX_CHANNELS = 8


def ant_address(dev_type: int, dev_num: int) -> str:
    return f"ant:{dev_type}:{dev_num}"


def parse_ant_address(address: str):
    """'ant:120:12345' -> (120, 12345), sonst None."""
    if not isinstance(address, str) or not address.startswith("ant:"):
        return None
    try:
        _, t, n = address.split(":")
        return int(t), int(n)
    except ValueError:
        return None


def ant_frame(msg_id: int, *data: int) -> bytes:
    body = bytes([0xA4, len(data), msg_id, *data])
    cs = 0
    for b in body:
        cs ^= b
    return body + bytes([cs])


def parse_ant_stream(buf: bytearray) -> list:
    """Zerlegt den Empfangspuffer in (msg_id, data)-Nachrichten. Verbraucht buf."""
    out = []
    while True:
        start = buf.find(0xA4)
        if start < 0:
            buf.clear()
            return out
        if start:
            del buf[:start]
        if len(buf) < 4:
            return out
        length = buf[1]
        total = length + 4
        if len(buf) < total:
            return out
        cs = 0
        for b in buf[:total - 1]:
            cs ^= b
        if cs != buf[total - 1]:
            del buf[0]  # kaputt -> nächstes Sync-Byte suchen
            continue
        out.append((buf[2], bytes(buf[3:total - 1])))
        del buf[:total]


class AntDecoder:
    """Wandelt 8-Byte-ANT+-Seiten je Gerätetyp in Werte um."""

    def __init__(self, dev_type: int):
        self.type = dev_type
        self.crank = CrankCadence()

    def decode(self, p: bytes, now: float) -> dict:
        t = self.type
        if t == 120:  # Herzfrequenz: Byte 7
            return {"hr": p[7]} if p[7] else {}
        if t == 11 and p[0] == 0x10:  # Leistung, Standard-Seite
            out = {"power": p[6] | p[7] << 8}
            if p[3] != 0xFF:
                out["cadence"] = p[3]
            return out
        if t == 17 and p[0] == 0x19:  # FE-C: Trainer-/Ergometerdaten
            out = {}
            if p[2] != 0xFF:
                out["cadence"] = p[2]
            power = p[5] | (p[6] & 0x0F) << 8
            if power != 0xFFF:
                out["power"] = power
            return out
        if t == 122:  # reiner Kadenzsensor
            return {"cadence": self.crank.update(p[6] | p[7] << 8, p[4] | p[5] << 8, now)}
        if t == 121:  # Speed+Cadence-Kombisensor
            return {"cadence": self.crank.update(p[2] | p[3] << 8, p[0] | p[1] << 8, now)}
        return {}


class AntError(Exception):
    pass


class AntStick:
    def __init__(self):
        try:
            import usb.core
            import usb.util
        except ImportError:
            raise AntError("Paket fehlt – bitte: py -m pip install pyusb libusb-package")
        self.usb = usb
        backend = None
        try:
            import libusb_package
            backend = libusb_package.get_libusb1_backend()
        except Exception:
            pass
        dev = None
        for vid, pid in ANT_STICKS:
            dev = usb.core.find(idVendor=vid, idProduct=pid, backend=backend)
            if dev is not None:
                break
        if dev is None:
            raise AntError("kein ANT+-Stick gefunden")
        try:
            dev.set_configuration()
        except usb.core.USBError as e:
            msg = str(e).lower()
            if "busy" in msg or "access" in msg or "not supported" in msg:
                raise AntError(f"Zugriff auf den Stick nicht möglich ({e}). Treiber nicht mit "
                               "Zadig auf WinUSB umgestellt oder Stick von anderem Programm belegt")
            # schon konfiguriert ist ok
        except NotImplementedError:
            raise AntError("Stick-Treiber passt nicht – mit Zadig auf WinUSB umstellen (siehe README)")
        intf = dev.get_active_configuration()[(0, 0)]
        direction = usb.util.endpoint_direction
        self.ep_out = usb.util.find_descriptor(
            intf, custom_match=lambda e: direction(e.bEndpointAddress) == usb.util.ENDPOINT_OUT)
        self.ep_in = usb.util.find_descriptor(
            intf, custom_match=lambda e: direction(e.bEndpointAddress) == usb.util.ENDPOINT_IN)
        self.dev = dev
        self.buf = bytearray()

    def send(self, msg_id: int, *data: int):
        self.ep_out.write(ant_frame(msg_id, *data), timeout=500)

    def read(self, timeout_ms=100) -> list:
        try:
            self.buf += bytes(self.ep_in.read(64, timeout=timeout_ms))
        except self.usb.core.USBTimeoutError:
            pass
        return parse_ant_stream(self.buf)

    def close(self):
        try:
            self.usb.util.dispose_resources(self.dev)
        except Exception:
            pass


class AntManager:
    """Hört ANT+-Sensoren ab. Gleiche Nachrichten in die Queue wie der BleManager."""

    def __init__(self, out_q: queue.Queue):
        import concurrent.futures
        self.Future = concurrent.futures.Future
        self.q = out_q
        self.cmds = queue.Queue()
        self.wanted = {}      # address -> set(roles)
        self.channels = {}    # ch -> {"address", "roles", "decoder", "live"}
        self.closed = set()
        self.scan_found = None
        self.stick = None
        self.last_error = ""
        threading.Thread(target=self._run, daemon=True).start()

    # --- Aufrufe aus der Oberfläche ---
    def apply(self, roles: dict):
        self.cmds.put(("apply", {r: d["address"] for r, d in roles.items()}))

    def scan(self, seconds=5.0):
        fut = self.Future()
        self.cmds.put(("scan", seconds, fut))
        return fut

    # --- Hilfen ---
    def _status(self, address, kind, msg):
        for r in self.wanted.get(address, ()):
            self.q.put(("status", r, address, kind, msg))

    def _set_wanted(self, mapping):
        wanted = {}
        for role, addr in mapping.items():
            wanted.setdefault(addr, set()).add(role)
        self.wanted = wanted

    # --- Thread ---
    def _run(self):
        while True:
            if not self.wanted and self.cmds.empty():
                self._wait(1.0)  # ohne ANT-Geräte und ohne Suche den Stick nicht anfassen
                continue
            try:
                self.stick = AntStick()
                self._init_stick()
            except Exception as e:
                self._drop_stick()
                err = str(e) or type(e).__name__
                self._fail_pending(err)
                for addr in self.wanted:
                    self._status(addr, "error", f"ANT+: {err}")
                self._wait(4.0)
                continue
            try:
                self._sync_channels()
                while True:
                    self._process_cmds()
                    for mid, data in self.stick.read(100):
                        self._handle(mid, data)
            except Exception as e:
                err = str(e) or type(e).__name__
                for addr in self.wanted:
                    self._status(addr, "error", f"ANT+: {err}")
                self._drop_stick()
                self._fail_pending(err)
                time.sleep(2)

    def _fail_pending(self, err):
        """Ohne Stick: Suchanfragen mit Fehler beenden, Zuordnungen übernehmen."""
        while True:
            try:
                cmd = self.cmds.get_nowait()
            except queue.Empty:
                return
            if cmd[0] == "scan":
                cmd[2].set_exception(AntError(err))
            elif cmd[0] == "apply":
                self._set_wanted(cmd[1])

    def _wait(self, seconds):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            try:
                cmd = self.cmds.get(timeout=0.2)
            except queue.Empty:
                continue
            if cmd[0] == "apply":
                self._set_wanted(cmd[1])
                for addr in self.wanted:
                    self._status(addr, "connecting", "ANT+ wird gestartet …")
            else:
                self.cmds.put(cmd)  # Suche: Stick (erneut) öffnen
            return

    def _drop_stick(self):
        if self.stick:
            self.stick.close()
        self.stick = None
        self.channels = {}

    def _init_stick(self):
        s = self.stick
        s.send(0x4A, 0x00)  # Reset
        deadline = time.monotonic() + 1.5
        while time.monotonic() < deadline:
            if any(mid == 0x6F for mid, _ in s.read(100)):
                break
        time.sleep(0.2)
        self._cmd(0x46, 0x00, *ANT_NETWORK_KEY)
        try:
            self._cmd(0x66, 0x00, 0x01)  # erweiterte Nachrichten (Gerätenummer mitsenden)
        except AntError:
            pass
        self.channels, self.closed = {}, set()

    def _cmd(self, msg_id, *data, timeout=1.0):
        self.stick.send(msg_id, *data)
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            for mid, d in self.stick.read(50):
                if mid == 0x40 and len(d) >= 3 and d[1] == msg_id:
                    if d[2] != 0:
                        raise AntError(f"Befehl 0x{msg_id:02X} abgelehnt (Code {d[2]})")
                    return
                self._handle(mid, d)
        raise AntError(f"keine Antwort auf Befehl 0x{msg_id:02X}")

    def _process_cmds(self):
        while True:
            try:
                cmd = self.cmds.get_nowait()
            except queue.Empty:
                return
            if cmd[0] == "apply":
                self._set_wanted(cmd[1])
                self._sync_channels()
            elif cmd[0] == "scan":
                self._scan(cmd[1], cmd[2])

    # --- Kanäle ---
    def _open_channel(self, ch, address):
        dev_type, dev_num = parse_ant_address(address)
        period = ANT_TYPES.get(dev_type, ("", 8192))[1]
        self._cmd(0x42, ch, 0x00, 0x00)                         # Kanal zuweisen (Empfang)
        self._cmd(0x51, ch, dev_num & 0xFF, dev_num >> 8, dev_type, 0)  # Geräte-ID
        self._cmd(0x43, ch, period & 0xFF, period >> 8)         # Periode
        self._cmd(0x45, ch, ANT_FREQ)                           # Funkfrequenz 2457 MHz
        for msg_id, val in ((0x44, 0x00), (0x63, 0xFF)):        # nur Suche mit niedriger Priorität, endlos
            try:
                self._cmd(msg_id, ch, val)
            except AntError:
                pass
        self._cmd(0x4B, ch)                                     # öffnen
        self.channels[ch] = {"address": address, "decoder": AntDecoder(dev_type), "live": False}
        self._status(address, "connecting", "ANT+ sucht …")

    def _close_channel(self, ch):
        self.closed.discard(ch)
        try:
            self._cmd(0x4C, ch)
            deadline = time.monotonic() + 1.0
            while ch not in self.closed and time.monotonic() < deadline:
                for mid, d in self.stick.read(50):
                    self._handle(mid, d)
            self._cmd(0x41, ch)
        except AntError:
            pass
        self.channels.pop(ch, None)

    def _sync_channels(self):
        if not self.stick:
            return
        for ch, info in list(self.channels.items()):
            if info["address"] not in self.wanted:
                self._close_channel(ch)
        open_addrs = {i["address"] for i in self.channels.values()}
        for addr in self.wanted:
            if addr in open_addrs or not parse_ant_address(addr):
                continue
            free = [c for c in range(ANT_MAX_CHANNELS) if c not in self.channels]
            if not free:
                self._status(addr, "error", "ANT+: keine freien Kanäle")
                continue
            self._open_channel(free[0], addr)

    def _scan(self, seconds, fut):
        for ch in list(self.channels):
            self._close_channel(ch)
        self.scan_found = {}
        try:
            self._cmd(0x42, 0, 0x00, 0x00)
            self._cmd(0x51, 0, 0, 0, 0, 0)  # alles annehmen
            self._cmd(0x45, 0, ANT_FREQ)
            self._cmd(0x5B, 0x00)            # Dauer-Scanmodus
            deadline = time.monotonic() + seconds
            while time.monotonic() < deadline:
                for mid, d in self.stick.read(100):
                    self._handle(mid, d)
            self._close_channel(0)
            found = self.scan_found
        except Exception as e:
            self.scan_found = None
            fut.set_exception(e)
            raise
        self.scan_found = None
        results = []
        for (dev_type, dev_num) in sorted(found):
            label = ANT_TYPES[dev_type][0]
            results.append({"address": ant_address(dev_type, dev_num),
                            "name": f"ANT+ {label} #{dev_num}",
                            "rssi": None, "uuids": [], "ant_type": dev_type})
        fut.set_result(results)
        self._sync_channels()

    # --- Empfang ---
    def _handle(self, mid, d):
        if mid in (0x4E, 0x4F) and len(d) >= 9:
            ch, payload, ext = d[0], d[1:9], d[9:]
            if self.scan_found is not None:
                if len(ext) >= 5 and ext[0] & 0x80:
                    dev_num = ext[1] | ext[2] << 8
                    dev_type = ext[3] & 0x7F
                    if dev_type in ANT_TYPES and dev_num:
                        self.scan_found[(dev_type, dev_num)] = True
                return
            info = self.channels.get(ch)
            if not info:
                return
            addr = info["address"]
            if not info["live"]:
                info["live"] = True
                self._status(addr, "connected", "ANT+ verbunden")
            try:
                values = info["decoder"].decode(payload, time.monotonic())
            except Exception:
                return
            now = time.monotonic()
            for r in self.wanted.get(addr, ()):
                if values.get(r) is not None:
                    self.q.put(("value", r, addr, values[r], now))
        elif mid == 0x40 and len(d) >= 3 and d[1] == 0x01:  # Kanal-Ereignis
            ch, event = d[0], d[2]
            if event == 0x07:  # Kanal geschlossen
                self.closed.add(ch)
            elif event == 0x08 and ch in self.channels:  # Signal verloren
                info = self.channels[ch]
                info["live"] = False
                self._status(info["address"], "connecting", "ANT+ Signal weg – sucht …")


# ---------------------------------------------------------------------------
# Farben
# ---------------------------------------------------------------------------

BG = "#eef0f5"
INK = "#252b3b"
TILE_OFF, TILE_OFF_FG = "#dde3f0", INK
TILE_WAIT, TILE_ON, TILE_ON_FG = "#d99a2b", "#2f9e5b", "#ffffff"
TILE_ERR = "#c8553d"
HR_ZONE_COLORS = ["#b8bec8", "#4fa3ff", "#3ddc84", "#ffb020", "#ff4d4d"]  # Z1 … Z5
HR_ZONE_DEFAULT_PCT = (0.60, 0.70, 0.80, 0.90)  # Beginn von Z2 … Z5 in % der HFmax
POWER_COLOR, CADENCE_COLOR = "#ffd23f", "#5fd3ff"
ALARM = "#ff3b3b"
HR_ICON = "#ff4d4d"
POWER_ZONES = [(0.55, "#b8bec8"),   # Z1 aktive Erholung
               (0.75, "#4fa3ff"),   # Z2 Grundlage
               (0.90, "#3ddc84"),   # Z3 Tempo
               (1.05, "#ffd23f"),   # Z4 Schwelle
               (1.20, "#ff8c1a"),   # Z5 VO2max
               (1.50, "#ff4d4d"),   # Z6 anaerob
               (99.0, "#c77dff")]   # Z7 neuromuskulär


def power_zone_color(watts, ftp):
    if watts is None or not ftp:
        return "#ffffff"
    for limit, color in POWER_ZONES:
        if watts < limit * ftp:
            return color
    return POWER_ZONES[-1][1]
KEY = "#010203"  # diese Farbe wird im Overlay vollständig durchsichtig


def hr_zone_limits(settings) -> list:
    """Untergrenzen von Z2…Z5 in bpm: eigene Werte oder aus HFmax berechnet."""
    z = settings.get("hr_zones")
    if isinstance(z, list) and len(z) == 4 and all(isinstance(v, int) for v in z) \
            and z == sorted(z) and len(set(z)) == 4:
        return z
    max_hr = settings.get("max_hr") or 180
    return [round(p * max_hr) for p in HR_ZONE_DEFAULT_PCT]


def hr_zone(hr, settings) -> int:
    """1 … 5"""
    zone = 1
    for limit in hr_zone_limits(settings):
        if hr >= limit:
            zone += 1
    return zone


def zone_color(hr, settings):
    return HR_ZONE_COLORS[hr_zone(hr, settings) - 1]


# ---------------------------------------------------------------------------
# Startmaske
# ---------------------------------------------------------------------------

class Tile:
    def __init__(self, parent, role, on_click, f):
        self.role = role
        self.frame = tk.Frame(parent, width=int(250 * f), height=int(180 * f), cursor="hand2")
        self.frame.pack_propagate(False)
        info = ROLES[role]
        self.icon = tk.Label(self.frame, text=info["icon"], font=("Segoe UI Symbol", 40))
        self.icon.place(relx=0.5, rely=0.32, anchor="center")
        self.value = tk.Label(self.frame, font=("Segoe UI", 18, "bold"))
        self.value.place(x=int(16 * f), rely=0.66, anchor="w")
        self.sub = tk.Label(self.frame, font=("Segoe UI", 9))
        self.sub.place(x=int(16 * f), rely=0.85, anchor="w")
        for w in (self.frame, self.icon, self.value, self.sub):
            w.bind("<Button-1>", lambda _e: on_click(role))

    def show(self, bg, fg, value, sub):
        for w in (self.frame, self.icon, self.value, self.sub):
            w.configure(bg=bg)
        for w in (self.icon, self.value, self.sub):
            w.configure(fg=fg)
        self.value.configure(text=value)
        self.sub.configure(text=sub)


class SetupView:
    def __init__(self, app):
        self.app = app
        root = app.root
        f = app.ui
        root.title("Trainings-Overlay – Geräte")
        root.configure(bg=BG)
        root.resizable(False, False)

        tk.Label(root, text="Geräte verbinden", bg=BG, fg=INK,
                 font=("Segoe UI", 18, "bold")).pack(anchor="w", padx=int(24 * f), pady=(int(20 * f), 0))
        tk.Label(root, text="Klicke auf eine Kachel, um ein Gerät zu suchen und auszuwählen.",
                 bg=BG, fg="#5a6275", font=("Segoe UI", 10)).pack(anchor="w", padx=int(24 * f))

        grid = tk.Frame(root, bg=BG)
        grid.pack(padx=int(24 * f), pady=int(16 * f))
        self.tiles = {}
        for i, role in enumerate(ROLES):
            t = Tile(grid, role, self.pick, f)
            t.frame.grid(row=0, column=i, padx=int(6 * f))
            self.tiles[role] = t

        bottom = tk.Frame(root, bg=BG)
        bottom.pack(fill="x", padx=int(24 * f), pady=(0, int(20 * f)))
        opts = tk.Frame(bottom, bg=BG)
        opts.pack(side="left", anchor="s")
        lbl = {"bg": BG, "fg": INK, "font": ("Segoe UI", 10)}
        hint = {"bg": BG, "fg": "#5a6275", "font": ("Segoe UI", 9)}
        py = int(3 * f)

        tk.Label(opts, text="HFmax:", **lbl).grid(row=0, column=0, sticky="w", pady=py)
        self.max_hr = tk.IntVar(value=app.settings["max_hr"])
        tk.Spinbox(opts, from_=120, to=220, width=5, textvariable=self.max_hr,
                   font=("Segoe UI", 10)).grid(row=0, column=1, sticky="w", padx=6)
        tk.Button(opts, text="HF-Zonen …", command=self.edit_zones, relief="flat",
                  bg="#dde3f0", fg=INK, activebackground="#cfd6e6", padx=10,
                  font=("Segoe UI", 9), cursor="hand2").grid(row=0, column=2, sticky="w")

        tk.Label(opts, text="Min. RPM:", **lbl).grid(row=1, column=0, sticky="w", pady=py)
        self.min_rpm = tk.IntVar(value=app.settings["min_rpm"])
        tk.Spinbox(opts, from_=0, to=150, increment=5, width=5, textvariable=self.min_rpm,
                   font=("Segoe UI", 10)).grid(row=1, column=1, sticky="w", padx=6)
        tk.Label(opts, text="(darunter rot und blinkend, 0 = aus)", **hint).grid(
            row=1, column=2, sticky="w")

        tk.Label(opts, text="Anordnung:", **lbl).grid(row=2, column=0, sticky="w", pady=py)
        self.layout = tk.StringVar(value=app.settings["layout"])
        lay = tk.Frame(opts, bg=BG)
        lay.grid(row=2, column=1, columnspan=2, sticky="w", padx=6)
        for value, text in (("vertical", "untereinander"), ("horizontal", "nebeneinander")):
            tk.Radiobutton(lay, text=text, value=value, variable=self.layout, bg=BG,
                           activebackground=BG, font=("Segoe UI", 10)).pack(side="left", padx=(0, 10))

        tk.Label(opts, text="FTP:", **lbl).grid(row=3, column=0, sticky="w", pady=py)
        self.ftp = tk.IntVar(value=app.settings["ftp"])
        tk.Spinbox(opts, from_=80, to=500, increment=5, width=5, textvariable=self.ftp,
                   font=("Segoe UI", 10)).grid(row=3, column=1, sticky="w", padx=6)
        tk.Label(opts, text="(Watt, für die Farben der Leistungszonen)", **hint).grid(
            row=3, column=2, sticky="w")

        tk.Label(opts, text="Schriftgröße:", **lbl).grid(row=5, column=0, sticky="w", pady=py)
        self.size_pct = tk.IntVar(value=int(round(app.settings["scale"] * 100)))
        tk.Spinbox(opts, from_=50, to=300, increment=10, width=5, textvariable=self.size_pct,
                   font=("Segoe UI", 10)).grid(row=5, column=1, sticky="w", padx=6)
        tk.Label(opts, text="% (im Overlay auch per Mausrad oder Rechtsklick)", **hint).grid(
            row=5, column=2, sticky="w")

        tk.Label(opts, text="Zusätzlich:", **lbl).grid(row=4, column=0, sticky="w", pady=py)
        extra = tk.Frame(opts, bg=BG)
        extra.grid(row=4, column=1, columnspan=2, sticky="w", padx=6)
        self.show_vars = {}
        for key, text in (("show_3s", "3s-Watt"), ("show_avg_power", "Ø-Watt"),
                          ("show_avg_hr", "Ø-HF"), ("show_hr_zone", "HF-Zone")):
            var = tk.BooleanVar(value=app.settings[key])
            self.show_vars[key] = var
            tk.Checkbutton(extra, text=text, variable=var, bg=BG, activebackground=BG,
                           font=("Segoe UI", 10)).pack(side="left", padx=(0, 10))

        self.next_btn = tk.Button(bottom, text="Weiter  →", command=app.show_overlay,
                                  font=("Segoe UI", 11, "bold"), bg=INK, fg="white",
                                  activebackground="#3a4258", activeforeground="white",
                                  relief="flat", padx=int(18 * f), pady=int(6 * f), cursor="hand2")
        self.next_btn.pack(side="right", anchor="s")

        root.protocol("WM_DELETE_WINDOW", app.quit)

    def pick(self, role):
        DevicePicker(self.app, role)

    def edit_zones(self):
        self.store_options()  # aktuelle HFmax aus dem Feld übernehmen
        HrZoneDialog(self.app)

    def store_options(self):
        s = self.app.settings
        for key, var in (("max_hr", self.max_hr), ("min_rpm", self.min_rpm), ("ftp", self.ftp)):
            try:
                s[key] = int(var.get())
            except (tk.TclError, ValueError):
                pass
        s["layout"] = self.layout.get()
        for key, var in self.show_vars.items():
            s[key] = bool(var.get())
        try:
            s["scale"] = min(3.0, max(0.5, int(self.size_pct.get()) / 100))
        except (tk.TclError, ValueError):
            pass

    def sync_options(self):
        s = self.app.settings
        self.max_hr.set(s["max_hr"])
        self.min_rpm.set(s["min_rpm"])
        self.layout.set(s["layout"])
        self.ftp.set(s["ftp"])
        self.size_pct.set(int(round(s["scale"] * 100)))
        for key, var in self.show_vars.items():
            var.set(s[key])

    def render(self):
        app = self.app
        for role, tile in self.tiles.items():
            info, st = ROLES[role], app.state[role]
            dev = app.roles.get(role)
            if not dev:
                tile.show(TILE_OFF, TILE_OFF_FG, info["title"], "Klicken, um Gerät zu suchen")
                continue
            name = dev["name"] or dev["address"]
            if st["conn"] == "connected":
                v = app.fresh_value(role)
                txt = f"{info['icon']} {v:.0f}" if v is not None else f"{info['icon']} --"
                tile.show(TILE_ON, TILE_ON_FG, txt, name)
            elif st["conn"] == "error":
                tile.show(TILE_ERR, TILE_ON_FG, info["title"], f"{name} · {st['msg']}")
            else:
                tile.show(TILE_WAIT, TILE_ON_FG, info["title"], f"{name} · {st['msg'] or 'verbinde …'}")
        state = "normal" if app.roles else "disabled"
        self.next_btn.configure(state=state, bg=INK if app.roles else "#9aa1b1")


class HrZoneDialog(tk.Toplevel):
    """HF-Zonen bearbeiten: Untergrenzen von Z2 … Z5 in bpm."""

    NAMES = ["Erholung", "Grundlage", "Tempo", "Schwelle", "Maximal"]

    def __init__(self, app):
        super().__init__(app.root)
        self.app = app
        self.title("HF-Zonen")
        self.configure(bg=BG, padx=18, pady=14)
        self.transient(app.root)
        self.resizable(False, False)
        lbl = {"bg": BG, "fg": INK, "font": ("Segoe UI", 10)}

        tk.Label(self, text="♥  HF-Zonen", bg=BG, fg=INK,
                 font=("Segoe UI", 14, "bold")).grid(row=0, column=0, columnspan=4, sticky="w")
        tk.Label(self, text="Ab welchem Puls beginnt die Zone? (bpm)", bg=BG, fg="#5a6275",
                 font=("Segoe UI", 9)).grid(row=1, column=0, columnspan=4, sticky="w", pady=(0, 8))

        self.vars = [tk.IntVar(value=v) for v in hr_zone_limits(app.settings)]
        self.ranges = []
        for i in range(5):
            row = i + 2
            tk.Label(self, text="  ", bg=HR_ZONE_COLORS[i], width=2).grid(row=row, column=0, pady=2)
            tk.Label(self, text=f"Z{i + 1}  {self.NAMES[i]}", width=14, anchor="w",
                     **lbl).grid(row=row, column=1, sticky="w", padx=(6, 0))
            if i == 0:
                tk.Label(self, text="ab 0", width=6, anchor="w", **lbl).grid(row=row, column=2)
            else:
                tk.Spinbox(self, from_=40, to=230, width=5, textvariable=self.vars[i - 1],
                           font=("Segoe UI", 10), command=self.update_ranges).grid(row=row, column=2)
            r = tk.Label(self, text="", bg=BG, fg="#5a6275", font=("Segoe UI", 9), width=18, anchor="w")
            r.grid(row=row, column=3, sticky="w", padx=6)
            self.ranges.append(r)
        for v in self.vars:
            v.trace_add("write", lambda *_: self.update_ranges())

        self.msg = tk.Label(self, text="", bg=BG, fg=TILE_ERR, font=("Segoe UI", 9))
        self.msg.grid(row=7, column=0, columnspan=4, sticky="w", pady=(6, 0))

        btns = tk.Frame(self, bg=BG)
        btns.grid(row=8, column=0, columnspan=4, sticky="ew", pady=(8, 0))
        tk.Button(btns, text="Aus HFmax berechnen", relief="flat", padx=10,
                  command=self.from_max).pack(side="left")
        tk.Button(btns, text="Speichern", command=self.save, bg=INK, fg="white", relief="flat",
                  padx=14, font=("Segoe UI", 10, "bold")).pack(side="right")
        tk.Button(btns, text="Abbrechen", command=self.destroy, relief="flat",
                  padx=10).pack(side="right", padx=6)
        self.update_ranges()
        try:
            self.grab_set()
        except tk.TclError:
            pass

    def values(self):
        try:
            return [int(v.get()) for v in self.vars]
        except (tk.TclError, ValueError):
            return None

    def update_ranges(self):
        vals = self.values()
        max_hr = self.app.settings["max_hr"]
        if not vals:
            return
        bounds = [0] + vals + [None]
        for i, r in enumerate(self.ranges):
            lo, hi = bounds[i], bounds[i + 1]
            pct = f"{lo / max_hr * 100:.0f} %" if lo else "0 %"
            r.configure(text=f"{lo}–{hi - 1} bpm ({pct})" if hi else f"ab {lo} bpm ({pct})")

    def from_max(self):
        max_hr = self.app.settings["max_hr"]
        for v, p in zip(self.vars, HR_ZONE_DEFAULT_PCT):
            v.set(round(p * max_hr))

    def save(self):
        vals = self.values()
        if not vals or vals != sorted(vals) or len(set(vals)) != 4:
            self.msg.configure(text="Die Werte müssen von Z2 bis Z5 ansteigen.")
            return
        defaults = [round(p * self.app.settings["max_hr"]) for p in HR_ZONE_DEFAULT_PCT]
        # Entsprechen die Werte genau der HFmax-Rechnung, bleiben sie an die HFmax gekoppelt
        self.app.settings["hr_zones"] = None if vals == defaults else vals
        save_settings(self.app.settings)
        self.destroy()


class DevicePicker(tk.Toplevel):
    def __init__(self, app, role):
        super().__init__(app.root)
        self.app, self.role = app, role
        self.results, self.items, self.notes, self._job = [], [], [], None
        info = ROLES[role]
        self.title(f"{info['title']}: Gerät auswählen")
        self.configure(bg=BG, padx=18, pady=14)
        self.transient(app.root)
        self.resizable(False, False)

        tk.Label(self, text=f"{info['icon']}  {info['title']}", bg=BG, fg=INK,
                 font=("Segoe UI", 14, "bold")).pack(anchor="w")
        tk.Label(self, text="Gerät einschalten bzw. Gurt anlegen. Geräte, die gerade mit einer\n"
                            "anderen App verbunden sind (z. B. Zwift), tauchen evtl. nicht auf.",
                 bg=BG, fg="#5a6275", justify="left", font=("Segoe UI", 9)).pack(anchor="w", pady=(2, 8))

        self.listbox = tk.Listbox(self, width=52, height=9, font=("Segoe UI", 10),
                                  activestyle="none", selectbackground=TILE_ON, relief="flat")
        self.listbox.pack(fill="x")
        self.listbox.bind("<Double-Button-1>", lambda _e: self.choose())

        self.show_all = tk.BooleanVar(value=False)
        tk.Checkbutton(self, text="Alle Bluetooth-Geräte zeigen", variable=self.show_all,
                       command=self.fill, bg=BG, activebackground=BG,
                       font=("Segoe UI", 9)).pack(anchor="w", pady=(4, 0))
        self.status = tk.Label(self, text="", bg=BG, fg="#5a6275", font=("Segoe UI", 9),
                               justify="left", wraplength=420)
        self.status.pack(anchor="w")

        btns = tk.Frame(self, bg=BG)
        btns.pack(fill="x", pady=(10, 0))
        self.scan_btn = tk.Button(btns, text="Neu suchen", command=self.scan, relief="flat", padx=10)
        self.scan_btn.pack(side="left")
        if role in app.roles:
            tk.Button(btns, text="Entfernen", relief="flat", padx=10,
                      command=lambda: (app.assign(role, None), self.destroy())).pack(side="left", padx=6)
        tk.Button(btns, text="Verbinden", command=self.choose, bg=INK, fg="white",
                  relief="flat", padx=14, font=("Segoe UI", 10, "bold")).pack(side="right")
        tk.Button(btns, text="Abbrechen", command=self.destroy, relief="flat",
                  padx=10).pack(side="right", padx=6)

        try:
            self.grab_set()
        except tk.TclError:
            pass
        self.scan()

    def destroy(self):
        if self._job:
            self.after_cancel(self._job)
            self._job = None
        super().destroy()

    def scan(self):
        self.status.configure(text="Suche Bluetooth und ANT+ … (5 Sekunden)")
        self.scan_btn.configure(state="disabled")
        futs = {"Bluetooth": self.app.ble.scan(), "ANT+": self.app.ant.scan()}
        self._job = self.after(200, self.check, futs)

    def check(self, futs):
        self._job = None
        if not all(f.done() for f in futs.values()):
            self._job = self.after(200, self.check, futs)
            return
        self.scan_btn.configure(state="normal")
        self.results, self.notes = [], []
        for label, fut in futs.items():
            try:
                self.results += fut.result()
            except Exception as e:
                self.notes.append(f"{label}: {e}")
        self.fill()

    def fill(self):
        wanted = ROLES[self.role]["services"]
        ant_types = ROLES[self.role]["ant"]
        items = [d for d in self.results
                 if self.show_all.get() or any(s in d["uuids"] for s in wanted)
                 or d.get("ant_type") in ant_types]
        if not self.show_all.get():
            items = [d for d in items if d["name"]] or items
        items.sort(key=lambda d: (d.get("ant_type") is not None,
                                  -(d["rssi"] if d["rssi"] is not None else -999)))
        # aktuell gewähltes oder anderswo schon verbundenes Gerät (sendet dann keine Werbung mehr)
        known = {d["address"]: d for d in self.app.roles.values()}
        for addr, dev in known.items():
            if addr not in {d["address"] for d in items}:
                cur = self.app.roles.get(self.role)
                if (cur and cur["address"] == addr) or self.show_all.get() or self._fits(addr):
                    items.insert(0, {"address": addr, "name": dev["name"], "rssi": None, "uuids": []})
        self.items = items
        self.listbox.delete(0, "end")
        current = self.app.roles.get(self.role, {}).get("address")
        for d in items:
            name = d["name"] or f"(ohne Namen) {d['address']}"
            if d.get("ant_type") is not None:
                extra = "ANT+"
            elif d["rssi"] is None:
                extra = "verbunden"
            else:
                extra = f"Bluetooth, {d['rssi']} dBm"
            mark = "✓ " if d["address"] == current else "   "
            self.listbox.insert("end", f"{mark}{name}   ({extra})")
        note = ("\n" + "\n".join(self.notes)) if self.notes else ""
        if not items:
            self.status.configure(text="Nichts gefunden. Gerät wecken und „Neu suchen“ klicken." + note)
        else:
            self.status.configure(
                text=f"{len(items)} Gerät(e) gefunden – auswählen und „Verbinden“." + note)
            self.listbox.selection_set(0)

    def _fits(self, address):
        # Ein bereits für Leistung verbundener Trainer passt auch für Kadenz und umgekehrt
        bike = {"power", "cadence"}
        return self.role in bike and any(
            r in bike and d["address"] == address for r, d in self.app.roles.items())

    def choose(self):
        sel = self.listbox.curselection()
        if not sel:
            return
        d = self.items[sel[0]]
        self.app.assign(self.role, d)
        # Trainer gewählt? Dann gleich für die andere Rad-Rolle mit anbieten
        other = {"power": "cadence", "cadence": "power"}.get(self.role)
        is_bike = any(s in d["uuids"] for s in (FTMS_SERVICE, CPS_SERVICE)) or \
            d.get("ant_type") in (17, 11)
        if other and other not in self.app.roles and is_bike:
            self.app.assign(other, d)
        self.destroy()


# ---------------------------------------------------------------------------
# Schwebendes Overlay
# ---------------------------------------------------------------------------

class OverlayView:
    def __init__(self, app):
        self.app = app
        s = app.settings
        self.top = top = tk.Toplevel(app.root)
        top.withdraw()
        top.overrideredirect(True)
        top.attributes("-topmost", True)
        try:
            top.attributes("-transparentcolor", KEY)  # nur unter Windows
        except tk.TclError:
            pass
        top.attributes("-alpha", s["alpha"])
        top.geometry(f"+{s['x']}+{s['y']}")
        self.canvas = tk.Canvas(top, bg=KEY, highlightthickness=0, cursor="fleur")
        self.canvas.pack()
        self.signature = None

        c = self.canvas
        c.bind("<ButtonPress-1>", self.drag_start)
        c.bind("<B1-Motion>", self.drag_move)
        c.bind("<ButtonRelease-1>", lambda _e: self.remember_pos())
        c.bind("<Button-3>", lambda e: self.menu.tk_popup(e.x_root, e.y_root))
        c.bind("<MouseWheel>", self.wheel)

        self.menu = tk.Menu(top, tearoff=0)
        self.menu.add_command(label="Hintergrund an/aus", command=self.toggle_panel)
        self.menu.add_command(label="Anordnung wechseln (neben-/untereinander)",
                              command=self.toggle_layout)
        sub = tk.Menu(self.menu, tearoff=0)
        for a in (1.0, 0.9, 0.75, 0.6, 0.45):
            sub.add_command(label=f"{int(a * 100)} %", command=lambda a=a: self.set_alpha(a))
        self.menu.add_cascade(label="Deckkraft", menu=sub)
        size = tk.Menu(self.menu, tearoff=0)
        for pct in (50, 75, 100, 125, 150, 200, 250, 300):
            size.add_command(label=f"{pct} %", command=lambda p=pct: self.set_scale(p / 100))
        self.menu.add_cascade(label="Schriftgröße", menu=size)
        self.menu.add_command(label="Durchschnitte zurücksetzen", command=self.reset_stats)
        self.menu.add_separator()
        self.menu.add_command(label="Geräte ändern …", command=app.show_setup)
        self.menu.add_command(label="Beenden", command=app.quit)

        self.keep_on_top()

    def show(self):
        self.signature = None
        self.top.deiconify()
        self.render()

    def hide(self):
        self.top.withdraw()

    def visible(self):
        return self.top.winfo_viewable()

    # --- Maus ---
    def drag_start(self, e):
        self._dx = e.x_root - self.top.winfo_x()
        self._dy = e.y_root - self.top.winfo_y()

    def drag_move(self, e):
        self.top.geometry(f"+{e.x_root - self._dx}+{e.y_root - self._dy}")

    def remember_pos(self):
        self.app.settings["x"], self.app.settings["y"] = self.top.winfo_x(), self.top.winfo_y()
        save_settings(self.app.settings)

    def wheel(self, e):
        s = self.app.settings
        s["scale"] = round(min(3.0, max(0.5, s["scale"] + (0.1 if e.delta > 0 else -0.1))), 2)
        self.signature = None
        self.render()
        save_settings(s)

    def toggle_panel(self):
        self.app.settings["panel"] = not self.app.settings["panel"]
        self.signature = None
        self.render()
        save_settings(self.app.settings)

    def set_scale(self, scale):
        self.app.settings["scale"] = scale
        self.signature = None
        self.render()
        save_settings(self.app.settings)

    def reset_stats(self):
        self.app.stats.reset()
        self.signature = None
        self.render()

    def toggle_layout(self):
        s = self.app.settings
        s["layout"] = "horizontal" if s["layout"] == "vertical" else "vertical"
        self.signature = None
        self.render()
        save_settings(s)

    def set_alpha(self, a):
        self.app.settings["alpha"] = a
        self.top.attributes("-alpha", a)
        save_settings(self.app.settings)

    def keep_on_top(self):
        if self.visible():
            self.top.attributes("-topmost", False)
            self.top.attributes("-topmost", True)
        self.top.after(2000, self.keep_on_top)

    # --- Zeichnen ---
    def lines(self):
        """Pro Wert: (Symbol, Symbolfarbe, Text, Textfarbe, Kürzel, blinkt, Zusatz hinter der Zahl)."""
        app, s, out = self.app, self.app.settings, []
        fmt = lambda v: f"{v:.0f}" if v is not None else "--"  # noqa: E731
        min_rpm = s.get("min_rpm") or 0
        ftp = s.get("ftp")

        if "hr" in app.roles:
            v = app.fresh_value("hr")
            color = zone_color(v, s) if v is not None else "#9aa0a6"
            zone = f"Z{hr_zone(v, s)}" if v is not None and s["show_hr_zone"] else ""
            out.append(("♥", HR_ICON, fmt(v), color, "", False, zone))
            if s["show_avg_hr"]:
                a = app.stats.avg_hr()
                out.append(("♥", HR_ICON, fmt(a), zone_color(a, s) if a else "#ffffff",
                            "Ø", False, ""))

        if "power" in app.roles:
            v = app.fresh_value("power")
            p3 = app.power_3s()
            # Zonenfarbe nach 3s-Watt, damit sie nicht bei jedem Tritt flackert
            color = power_zone_color(p3 if p3 is not None else v, ftp) if v is not None else "#9aa0a6"
            out.append(("⚡", POWER_COLOR, fmt(v), color, "", False, ""))
            if s["show_3s"]:
                out.append(("⚡", POWER_COLOR, fmt(p3), power_zone_color(p3, ftp), "3s", False, ""))
            if s["show_avg_power"]:
                a = app.stats.avg_power()
                out.append(("⚡", POWER_COLOR, fmt(a), power_zone_color(a, ftp), "Ø", False, ""))

        if "cadence" in app.roles:
            v = app.fresh_value("cadence")
            # Alarm nur beim Treten (0 rpm = Pause, kein Alarm)
            low = v is not None and min_rpm > 0 and 0 < round(v) < min_rpm
            out.append(("↻", ALARM if low else CADENCE_COLOR, fmt(v),
                        ALARM if low else "#ffffff", "", low, ""))
        return out

    def render(self):
        lines = self.lines()
        s = self.app.settings
        blink_on = int(time.monotonic() * 2.5) % 2 == 0
        if any(l[5] for l in lines):
            blink = blink_on
        else:
            blink = None
        sig = (tuple(lines), s["scale"], s["panel"], s["layout"], blink)
        if sig == self.signature:
            return
        self.signature = sig
        c = self.canvas
        c.delete("all")
        f = s["scale"] * self.app.ui
        big = tkfont.Font(family="Segoe UI", size=int(34 * s["scale"]), weight="bold")
        small = tkfont.Font(family="Segoe UI", size=int(13 * s["scale"]), weight="bold")
        icon_font = tkfont.Font(family="Segoe UI Symbol", size=int(24 * s["scale"]))
        pad, gap = int(12 * f), int(8 * f)
        icon_w = max(icon_font.measure(i) for i in "♥⚡↻")
        line_h = big.metrics("linespace")
        horizontal = s["layout"] == "horizontal"
        num_w = big.measure("000")  # feste Breite, damit nebeneinander nichts springt
        # Kürzel-Spalte (Ø, 3s): untereinander gleich breit, damit die Zahlen bündig stehen
        tags = [l[4] for l in lines if l[4]]
        tag_col = (max(small.measure(t) for t in tags) + int(4 * f)) if tags else 0

        x0, y, width = pad, pad, 0
        for icon, icon_col, text, col, tag, blinking, suffix in lines:
            hidden = blinking and not blink_on
            cy = y + line_h // 2
            self.outlined(x0 + icon_w // 2, cy, icon, icon_font, icon_col, "center")
            x = x0 + icon_w + int(2 * f)
            if tag:
                self.outlined(x, cy + int(8 * f), tag, small, "#d0d4dc", "w")
            if horizontal:
                x += (small.measure(tag) + int(4 * f) if tag else 0) + gap
            else:
                x += tag_col + gap
            if not hidden:
                self.outlined(x, cy, text, big, col, "w")
            tw = big.measure(text)
            x += max(tw, num_w) if horizontal else tw
            if suffix:
                x += int(6 * f)
                self.outlined(x, cy + int(8 * f), suffix, small, col, "w")
                x += small.measure(suffix)
            width = max(width, x)
            if horizontal:
                x0 = x + int(30 * f)
            else:
                y += line_h
        if lines and horizontal:
            y += line_h
        if not lines:
            self.outlined(pad, pad, "keine Geräte", small, "#ffffff", "nw")
            width, y = small.measure("keine Geräte") + pad, pad + small.metrics("linespace")
        c.configure(width=width + pad, height=y + pad, bg="#141414" if s["panel"] else KEY)

    def outlined(self, x, y, text, font, color, anchor):
        o = max(1, int(2 * self.app.settings["scale"]))
        for dx in (-o, 0, o):
            for dy in (-o, 0, o):
                if dx or dy:
                    self.canvas.create_text(x + dx, y + dy, text=text, font=font,
                                            fill="#000000", anchor=anchor)
        self.canvas.create_text(x, y, text=text, font=font, fill=color, anchor=anchor)


# ---------------------------------------------------------------------------
# App
# ---------------------------------------------------------------------------

class Stats:
    """Ø-Watt, Ø-HF (zeitgewichtet, 1 Wert pro Sekunde) und gleitende 3s-Watt."""

    def __init__(self):
        self.reset()

    def reset(self):
        self.p_sum = self.p_n = self.hr_sum = self.hr_n = 0
        self.p_started = False
        self.recent = collections.deque()

    def add_power(self, watts, t):
        self.recent.append((t, watts))
        while self.recent and t - self.recent[0][0] > 3.0:
            self.recent.popleft()

    def power_3s(self, now):
        vals = [w for t, w in self.recent if now - t < 3.0]
        return sum(vals) / len(vals) if vals else None

    def tick(self, power, hr):
        if power is not None:
            if power > 0:
                self.p_started = True  # Ø erst ab dem ersten Tritt, Rollphasen zählen danach mit
            if self.p_started:
                self.p_sum += power
                self.p_n += 1
        if hr:
            self.hr_sum += hr
            self.hr_n += 1

    def avg_power(self):
        return self.p_sum / self.p_n if self.p_n else None

    def avg_hr(self):
        return self.hr_sum / self.hr_n if self.hr_n else None


class App:
    def __init__(self):
        if sys.platform == "win32":
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except Exception:
                pass
        self.settings = load_settings()
        self.q = queue.Queue()
        self.ble = BleManager(self.q)
        self.ant = AntManager(self.q)
        self.stats = Stats()
        self.root = tk.Tk()
        self.ui = max(1.0, float(self.root.tk.call("tk", "scaling")) / 1.333)
        self.state = {r: {"value": None, "t": 0.0, "conn": None, "msg": ""} for r in ROLES}
        self.roles = {r: d for r, d in self.settings.get("devices", {}).items()
                      if r in ROLES and isinstance(d, dict) and d.get("address")}
        self.setup = SetupView(self)
        self.overlay = OverlayView(self)
        if self.roles:
            self.apply_roles()
        self.root.after(150, self.poll)
        self.root.after(1000, self.tick)

    def assign(self, role, device):
        if device is None:
            self.roles.pop(role, None)
        else:
            self.roles[role] = {"address": device["address"], "name": device["name"]}
        self.state[role] = {"value": None, "t": 0.0, "conn": None, "msg": ""}
        self.settings["devices"] = self.roles
        save_settings(self.settings)
        self.apply_roles()

    def apply_roles(self):
        ant = {r: d for r, d in self.roles.items() if parse_ant_address(d["address"])}
        ble = {r: d for r, d in self.roles.items() if r not in ant}
        self.ble.apply(ble)
        self.ant.apply(ant)

    def fresh_value(self, role):
        st = self.state[role]
        if st["value"] is not None and time.monotonic() - st["t"] < STALE_SECONDS:
            return st["value"]
        return None

    def poll(self):
        try:
            while True:
                msg = self.q.get_nowait()
                kind, role, addr = msg[0], msg[1], msg[2]
                dev = self.roles.get(role)
                if not dev or dev["address"] != addr:
                    continue  # alte Meldung eines abgewählten Geräts
                st = self.state[role]
                if kind == "value":
                    st["value"], st["t"] = msg[3], msg[4]
                    if role == "power":
                        self.stats.add_power(msg[3], msg[4])
                    st["conn"] = "connected"
                else:
                    st["conn"], st["msg"] = msg[3], msg[4]
        except queue.Empty:
            pass
        if self.overlay.visible():
            self.overlay.render()
        else:
            self.setup.render()
        self.root.after(200, self.poll)

    def tick(self):
        self.stats.tick(self.fresh_value("power"), self.fresh_value("hr"))
        self.root.after(1000, self.tick)

    def power_3s(self):
        if self.fresh_value("power") is None:
            return None
        return self.stats.power_3s(time.monotonic())

    def show_overlay(self):
        if not self.roles:
            return
        self.setup.store_options()
        save_settings(self.settings)
        self.root.withdraw()
        self.overlay.show()

    def show_setup(self):
        self.overlay.hide()
        self.setup.sync_options()
        self.root.deiconify()
        self.root.lift()

    def quit(self):
        save_settings(self.settings)
        self.root.destroy()

    def run(self):
        import signal
        signal.signal(signal.SIGINT, lambda *_: self.root.after(0, self.quit))

        def tick():  # damit Strg+C in der Konsole ankommt
            self.root.after(250, tick)
        tick()
        try:
            self.root.mainloop()
        except KeyboardInterrupt:
            pass


def ant_test(seconds=8):
    """Diagnose: py overlay.py --ant-test"""
    print("ANT+-Test")
    print("=========")
    try:
        import usb.core  # noqa: F401
        print("[ok]   pyusb installiert")
    except ImportError:
        print("[FEHLER] pyusb fehlt  ->  py -m pip install pyusb libusb-package")
        return
    try:
        import libusb_package
        print(f"[ok]   libusb-package installiert ({libusb_package.get_library_path()})")
    except Exception as e:
        print(f"[WARN] libusb-package fehlt/defekt: {e}  ->  py -m pip install libusb-package")
    try:
        stick = AntStick()
        print("[ok]   ANT+-Stick gefunden und geöffnet")
    except Exception as e:
        print(f"[FEHLER] Stick: {e}")
        print("         Häufige Ursachen: Treiber nicht mit Zadig auf WinUSB umgestellt,")
        print("         oder MyWhoosh/Zwift/Garmin Express benutzt den Stick gerade.")
        return

    def cmd(mid, *data):
        stick.send(mid, *data)
        end = time.monotonic() + 1.0
        while time.monotonic() < end:
            for m, d in stick.read(50):
                if m == 0x40 and len(d) >= 3 and d[1] == mid:
                    return d[2]
        return None

    stick.send(0x4A, 0)
    end = time.monotonic() + 1.5
    reset_ok = False
    while time.monotonic() < end and not reset_ok:
        reset_ok = any(m == 0x6F for m, _ in stick.read(100))
    print("[ok]   Reset" if reset_ok else "[WARN] keine Reset-Antwort (meist trotzdem ok)")
    print("[ok]   Netzwerkschlüssel" if cmd(0x46, 0, *ANT_NETWORK_KEY) == 0
          else "[FEHLER] Netzwerkschlüssel abgelehnt")
    ext = cmd(0x66, 0, 1)
    print("[ok]   erweiterte Nachrichten" if ext == 0
          else f"[WARN] erweiterte Nachrichten nicht unterstützt (Antwort {ext})")
    cmd(0x42, 0, 0, 0)
    cmd(0x51, 0, 0, 0, 0, 0)
    cmd(0x45, 0, ANT_FREQ)
    scan = cmd(0x5B, 0)
    print("[ok]   Scanmodus gestartet" if scan == 0 else f"[FEHLER] Scanmodus abgelehnt (Antwort {scan})")
    print(f"\nHöre {seconds} Sekunden mit (Gurt anlegen, kurz in die Pedale treten) …")
    raw, found = 0, {}
    end = time.monotonic() + seconds
    while time.monotonic() < end:
        for m, d in stick.read(100):
            if m in (0x4E, 0x4F):
                raw += 1
                ext_d = d[9:]
                if len(ext_d) >= 5 and ext_d[0] & 0x80:
                    key = (ext_d[3] & 0x7F, ext_d[1] | ext_d[2] << 8)
                    found[key] = found.get(key, 0) + 1
    stick.send(0x4A, 0)
    stick.close()
    print(f"\nEmpfangene ANT+-Datenpakete: {raw}")
    for (t, n), c in sorted(found.items()):
        print(f"   Typ {t:>3} ({ANT_TYPES.get(t, ('unbekannt',))[0]}), Nummer {n}: {c} Pakete")
    if raw == 0:
        print("Nichts empfangen: Stick näher ans Rad (Verlängerungskabel), Gurt anfeuchten.")
    elif not found:
        print("Pakete kommen an, aber ohne Gerätenummer -> bitte diese Ausgabe an Claude schicken.")


if __name__ == "__main__":
    if "--ant-test" in sys.argv:
        ant_test()
    else:
        App().run()
