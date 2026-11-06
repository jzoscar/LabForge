"""Simulated device lab on localhost: ARM sensor devices, each with a Redfish BMC.

Lets the whole flow - IaC apply, BMC power control, monitoring, rig checkout,
self-healing - run end to end without hardware. Stdlib only.

Each device serves:
  http://127.0.0.1:<http_port>   GET /version, GET /health,
                                 POST /firmware {"version": ..., "sha256": ..., "size": ...}
  http://127.0.0.1:<bmc_port>    Redfish subset: Systems/1 (PowerState, Status.Health),
                                 Actions/ComputerSystem.Reset, Chassis/1/Thermal
A control API injects faults:
  http://127.0.0.1:<control_port>/sim                       GET   device states
  http://127.0.0.1:<control_port>/sim/devices/<name>/fault  POST  {"fault": "crash|hang|overheat|none"}
A switched PDU lives on the control port too (outlet N powers the Nth device):
  http://127.0.0.1:<control_port>/pdu/outlets/<n>           POST  {"action": "on|off|cycle"}  (basic auth)

Faults model what goes wrong on real rigs: "crash" (firmware dead, hardware fine - a
power cycle fixes it), "hang" (device stops answering in time - a power cycle fixes it),
"overheat" (hardware problem - survives reboots, needs a human). A firmware version ending in
"-broken" models a bad build: it crashes on every boot until another version is flashed.
"""

from __future__ import annotations

import base64
import json
import threading
import time
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

FAULTS = {"crash", "hang", "overheat"}
BOOT_SECONDS = 2.0  # default; tests use a shorter boot
HANG_SECONDS = 10.0


@dataclass
class DeviceSpec:
    name: str
    http_port: int
    bmc_port: int
    firmware: str = "1.4.2"


@dataclass
class Device:
    spec: DeviceSpec
    serial: str
    power: str = "On"
    fault: str | None = None
    firmware: str = ""
    artifact_sha256: str | None = None  # of the last image flashed, so CI can prove its build arrived
    booted_at: float = field(default_factory=time.time)
    boot_seconds: float = BOOT_SECONDS
    lock: threading.Lock = field(default_factory=threading.Lock, repr=False)

    def __post_init__(self) -> None:
        self.firmware = self.firmware or self.spec.firmware

    @property
    def booting(self) -> bool:
        return time.time() - self.booted_at < self.boot_seconds

    @property
    def temperature(self) -> float:
        return 93.0 if self.fault == "overheat" else (41.0 if self.power == "On" else 24.0)

    def reboot(self) -> None:
        with self.lock:
            self.power = "On"
            self.booted_at = time.time()
            if self.fault in ("crash", "hang"):  # software faults clear on a power cycle
                self.fault = None

    def state(self) -> dict:
        return {"name": self.spec.name, "serial": self.serial, "power": self.power, "fault": self.fault,
                "firmware": self.firmware, "artifactSha256": self.artifact_sha256, "booting": self.booting, "temperature": self.temperature,
                "http": f"http://127.0.0.1:{self.spec.http_port}", "bmc": f"http://127.0.0.1:{self.spec.bmc_port}"}


class _Handler(BaseHTTPRequestHandler):
    server_version = "LabForgeSim/1.0"

    def log_message(self, *args) -> None:  # keep test and demo output quiet
        pass

    def _json(self, status: int, body: dict) -> None:
        data = json.dumps(body).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def _body(self) -> dict:
        length = int(self.headers.get("Content-Length") or 0)
        try:
            return json.loads(self.rfile.read(length) or b"{}")
        except json.JSONDecodeError:
            return {}

    def _drop(self) -> None:
        """Behave like a dead network stack: close without answering."""
        self.close_connection = True
        try:
            self.connection.shutdown(2)
        except OSError:
            pass


class _DeviceHandler(_Handler):
    device: Device

    def _unavailable(self) -> bool:
        d = self.device
        if d.power != "On" or d.booting or d.fault == "crash" or d.firmware.endswith("-broken"):
            self._drop()
            return True
        if d.fault == "hang":
            time.sleep(HANG_SECONDS)
            self._drop()
            return True
        return False

    def do_GET(self) -> None:
        if self._unavailable():
            return
        d = self.device
        if self.path == "/version":
            self._json(200, {"name": d.spec.name, "firmware": d.firmware, "serial": d.serial,
                             "artifact_sha256": d.artifact_sha256, "uptime_s": int(time.time() - d.booted_at)})
        elif self.path == "/health":
            if d.fault == "overheat":
                self._json(503, {"status": "overheat", "temperature": d.temperature})
            else:
                self._json(200, {"status": "ok", "temperature": d.temperature})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        # The updater lives in a recovery partition (A/B): it takes a new image even when the running
        # firmware crashed, as long as the device has power and is not hung.
        d = self.device
        if self.path == "/firmware" and d.power == "On" and not d.booting and d.fault != "hang":
            pass
        elif self._unavailable():
            return
        if self.path != "/firmware":
            self._json(404, {"error": "not found"})
            return
        body = self._body()
        version = str(body.get("version") or "")
        if not version:
            self._json(400, {"error": "version required"})
            return
        self.device.firmware = version
        self.device.artifact_sha256 = body.get("sha256")
        self.device.reboot()
        self._json(202, {"firmware": version, "rebooting": True})


class _BmcHandler(_Handler):
    device: Device
    credentials: str

    def _authorized(self) -> bool:
        expected = "Basic " + base64.b64encode(self.credentials.encode()).decode()
        if self.headers.get("Authorization") == expected:
            return True
        self.send_response(401)
        self.send_header("WWW-Authenticate", 'Basic realm="sim-bmc"')
        self.send_header("Content-Length", "0")
        self.end_headers()
        return False

    def do_GET(self) -> None:
        if not self._authorized():
            return
        d = self.device
        if self.path == "/redfish/v1/Systems":
            self._json(200, {"Members": [{"@odata.id": "/redfish/v1/Systems/1"}]})
        elif self.path == "/redfish/v1/Systems/1":
            health = "Warning" if d.fault == "overheat" else "OK"
            self._json(200, {
                "Id": "1", "Name": d.spec.name, "SerialNumber": d.serial, "PowerState": d.power,
                "Status": {"State": "Enabled" if d.power == "On" else "StandbyOffline", "Health": health},
                "Links": {"Chassis": [{"@odata.id": "/redfish/v1/Chassis/1"}]},
            })
        elif self.path == "/redfish/v1/Chassis/1/Thermal":
            self._json(200, {"Temperatures": [{"Name": "SoC", "ReadingCelsius": d.temperature}]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        if not self._authorized():
            return
        if self.path != "/redfish/v1/Systems/1/Actions/ComputerSystem.Reset":
            self._json(404, {"error": "not found"})
            return
        kind = self._body().get("ResetType")
        d = self.device
        if kind == "On":
            if d.power != "On":
                d.reboot()
        elif kind in ("ForceOff", "GracefulShutdown"):
            d.power = "Off"
        elif kind in ("ForceRestart", "GracefulRestart", "PowerCycle"):
            d.reboot()
        else:
            self._json(400, {"error": f"unsupported ResetType {kind!r}"})
            return
        self._json(200, {"ResetType": kind, "PowerState": d.power})


class _ControlHandler(_Handler):
    sim: Simulator

    def _pdu(self, outlet: str) -> None:
        expected = "Basic " + base64.b64encode(self.sim.bmc_credentials.encode()).decode()
        if self.headers.get("Authorization") != expected:
            self._json(401, {"error": "unauthorized"})
            return
        devices = list(self.sim.devices.values())
        if not outlet.isdigit() or not 1 <= int(outlet) <= len(devices):
            self._json(404, {"error": f"no outlet {outlet}"})
            return
        device, action = devices[int(outlet) - 1], self._body().get("action")
        if action == "off":
            device.power = "Off"
        elif action in ("on", "cycle"):
            device.reboot()
        else:
            self._json(400, {"error": "action must be on, off or cycle"})
            return
        self._json(200, {"outlet": int(outlet), "device": device.spec.name, "power": device.power})

    def do_GET(self) -> None:
        if self.path == "/sim":
            self._json(200, {"devices": [d.state() for d in self.sim.devices.values()]})
        else:
            self._json(404, {"error": "not found"})

    def do_POST(self) -> None:
        parts = self.path.strip("/").split("/")
        if len(parts) == 3 and parts[:2] == ["pdu", "outlets"]:
            self._pdu(parts[2])
            return
        if len(parts) == 4 and parts[:2] == ["sim", "devices"] and parts[3] == "fault":
            try:
                self.sim.inject(parts[2], self._body().get("fault"))
            except KeyError:
                self._json(404, {"error": f"unknown device {parts[2]!r}"})
                return
            except ValueError as e:
                self._json(400, {"error": str(e)})
                return
            self._json(200, self.sim.devices[parts[2]].state())
        else:
            self._json(404, {"error": "not found"})


class Simulator:
    def __init__(self, devices: list[DeviceSpec], control_port: int, bmc_credentials: str = "admin:admin",
                 boot_seconds: float = BOOT_SECONDS):
        self.devices = {s.name: Device(s, serial=f"SIM{i:05d}", boot_seconds=boot_seconds)
                        for i, s in enumerate(devices, 1)}
        self.control_port = control_port
        self.bmc_credentials = bmc_credentials
        self._servers: list[ThreadingHTTPServer] = []

    def inject(self, name: str, fault: str | None) -> None:
        device = self.devices[name]
        if fault in (None, "none", ""):
            device.fault = None
        elif fault in FAULTS:
            device.fault = fault
        else:
            raise ValueError(f"fault must be one of {sorted(FAULTS)} or none")

    def _serve(self, port: int, handler: type[BaseHTTPRequestHandler], **attrs) -> None:
        cls = type(handler.__name__, (handler,), attrs)
        server = ThreadingHTTPServer(("127.0.0.1", port), cls)
        server.daemon_threads = True
        self._servers.append(server)
        threading.Thread(target=server.serve_forever, daemon=True).start()

    def start(self) -> Simulator:
        for d in self.devices.values():
            self._serve(d.spec.http_port, _DeviceHandler, device=d)
            self._serve(d.spec.bmc_port, _BmcHandler, device=d, credentials=self.bmc_credentials)
        self._serve(self.control_port, _ControlHandler, sim=self)
        return self

    def stop(self) -> None:
        for s in self._servers:
            s.shutdown()
            s.server_close()
        self._servers.clear()


def default_devices(count: int = 3, base_port: int = 9100) -> list[DeviceSpec]:
    """sim-sensor-01 -> http 9111 / bmc 9112, sim-sensor-02 -> 9121 / 9122, ...
    The default 4th device is a budget board: declared in IaC without a BMC, reset through PDU outlet 4."""
    return [DeviceSpec(f"sim-sensor-{i:02d}", base_port + i * 10 + 1, base_port + i * 10 + 2)
            for i in range(1, count + 1)]
