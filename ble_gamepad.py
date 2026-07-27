# coding: utf-8

'''
BLE HID Gamepad — Pythonista / objc_util
=========================================
Turns your iPhone/iPad into a Bluetooth gamepad with a PS4-style layout.
Pairs with PC (Windows/macOS/Linux) or another iPad as a standard HID gamepad.

Layout:
  Left stick  — virtual joystick (LX / LY axes)
  Right stick — virtual joystick (RX / RY axes)
  L2 / R2     — analog trigger sliders (0-255)
  D-pad       — hat switch (8 directions + centre)
  Face buttons — Square / Cross / Circle / Triangle
  Shoulder    — L1 / R1
  System      — Share / Options / PS / Touchpad

HID Report (9 bytes, Report ID 1):
  [0]  LX   0-255 (centre=128)
  [1]  LY   0-255 (centre=128)
  [2]  RX   0-255 (centre=128)
  [3]  RY   0-255 (centre=128)
  [4]  L2 trigger 0-255
  [5]  R2 trigger 0-255
  [6]  Hat (bits 0-3)  + Square/Cross/Circle/Triangle (bits 4-7)
  [7]  L1 R1 L2btn R2btn Share Options L3 R3  (1 bit each)
  [8]  PS Touchpad + 6 padding bits

NOTE: BLE transmission is DISABLED in this build.
      All commands that would be sent over BLE are written to sharpbluesend.py
      instead, so they can be inspected, replayed, or forwarded by another process.
'''

import struct, time, math, threading, socket, zlib, datetime, json, os
import ui, console
from objc_util import *

load_framework('CoreBluetooth')

CBPeripheralManager     = ObjCClass('CBPeripheralManager')
CBMutableService        = ObjCClass('CBMutableService')
CBMutableCharacteristic = ObjCClass('CBMutableCharacteristic')
CBMutableDescriptor     = ObjCClass('CBMutableDescriptor')
CBUUID                  = ObjCClass('CBUUID')
NSData                  = ObjCClass('NSData')
NSArray                 = ObjCClass('NSArray')


# ── UUID helpers ──────────────────────────────────────────────────────────────

def uuid(s): return CBUUID.UUIDWithString_(s)

U_HID_SVC    = uuid('1812')
U_HID_INFO   = uuid('2A4A')
U_HID_CTRL   = uuid('2A4C')
U_REPORT_MAP = uuid('2A4B')
U_REPORT     = uuid('2A4D')
U_PROTO_MODE = uuid('2A4E')
U_BATT_SVC   = uuid('180F')
U_BATT_LEVEL = uuid('2A19')
U_DEVINFO    = uuid('180A')
U_MANUF      = uuid('2A29')
U_MODEL      = uuid('2A24')
U_REPORT_REF = uuid('2908')
# Custom service for remote touchpad (mouse control)
U_MOUSE_SVC  = uuid('A000B000-C000-D000-E000-F00000000001')
U_MOUSE_CHAR = uuid('A000B000-C000-D000-E000-F00000000002')


# ── HID Report Descriptor — Gamepad ──────────────────────────────────────────

HID_DESCRIPTOR = bytes([
    0x05, 0x01,        # Usage Page (Generic Desktop)
    0x09, 0x05,        # Usage (Gamepad)
    0xA1, 0x01,        # Collection (Application)
    0x85, 0x01,        #   Report ID 1

    # ── 4 analog axes (LX, LY, RX, RY) ─────────────────────────────────────
    0x09, 0x30,        #   Usage (X)
    0x09, 0x31,        #   Usage (Y)
    0x09, 0x32,        #   Usage (Z)   — right stick X
    0x09, 0x35,        #   Usage (Rz)  — right stick Y
    0x15, 0x00,        #   Logical Minimum (0)
    0x26, 0xFF, 0x00,  #   Logical Maximum (255)
    0x75, 0x08,        #   Report Size (8 bits)
    0x95, 0x04,        #   Report Count (4)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)

    # ── L2 / R2 analog triggers ──────────────────────────────────────────────
    0x09, 0x33,        #   Usage (Rx) — L2
    0x09, 0x34,        #   Usage (Ry) — R2
    0x15, 0x00,
    0x26, 0xFF, 0x00,
    0x75, 0x08,
    0x95, 0x02,
    0x81, 0x02,

    # ── Hat switch (D-pad, 8 directions + centre) ────────────────────────────
    0x09, 0x39,        #   Usage (Hat switch)
    0x15, 0x00,        #   Logical Minimum (0)
    0x25, 0x07,        #   Logical Maximum (7)
    0x35, 0x00,        #   Physical Minimum (0)
    0x46, 0x3B, 0x01,  #   Physical Maximum (315)
    0x65, 0x14,        #   Unit (Degrees)
    0x75, 0x04,        #   Report Size (4 bits)
    0x95, 0x01,        #   Report Count (1)
    0x81, 0x42,        #   Input (Data, Variable, Absolute, Null)

    # ── 4-bit padding ────────────────────────────────────────────────────────
    0x75, 0x04,
    0x95, 0x01,
    0x81, 0x03,        #   Input (Constant)

    # ── 14 buttons ──────────────────────────────────────────────────────────
    # Square Cross Circle Triangle L1 R1 L2 R2 Share Options L3 R3 PS Touch
    0x05, 0x09,        #   Usage Page (Button)
    0x19, 0x01,        #   Usage Minimum (Button 1)
    0x29, 0x0E,        #   Usage Maximum (Button 14)
    0x15, 0x00,
    0x25, 0x01,
    0x75, 0x01,        #   Report Size (1 bit)
    0x95, 0x0E,        #   Report Count (14)
    0x81, 0x02,        #   Input (Data, Variable, Absolute)

    # ── 2-bit padding ────────────────────────────────────────────────────────
    0x75, 0x02,
    0x95, 0x01,
    0x81, 0x03,

    0xC0               # End Collection
])


# ── Shared state ──────────────────────────────────────────────────────────────

pm            = [None]
report_char   = [None]
mouse_char    = [None]   # custom characteristic for remote touchpad
subscribed    = [False]
status_cb     = [None]
dolphin_dsu   = [None]   # DolphinDSU instance when Wi-Fi mode is active

# Gamepad state
state = {
    'lx': 128, 'ly': 128,   # left stick  (0-255)
    'rx': 128, 'ry': 128,   # right stick (0-255)
    'l2': 0,   'r2': 0,     # triggers    (0-255)
    'hat': 8,               # hat: 0=N 1=NE 2=E 3=SE 4=S 5=SW 6=W 7=NW 8=none
    # buttons (bool): square cross circle triangle l1 r1 l2btn r2btn
    #                 share options l3 r3 ps touch
    'btns': [False] * 14,
}


def nsdata(b): return NSData.dataWithBytes_length_(b, len(b))


# ── sharpbluesend.py redirect ─────────────────────────────────────────────────
#
# Instead of transmitting BLE HID reports or mouse packets over the air,
# every outgoing command is appended to sharpbluesend.py as a Python list
# of dicts.  Another process (e.g. sharpblue.csx via a file watcher) can
# read and forward or replay the entries.
#
# File format:
#   pending = [
#       {"ts": "<ISO>", "type": "gamepad", "report": [LX,LY,RX,RY,L2,R2,hat,btn_lo,btn_hi]},
#       {"ts": "<ISO>", "type": "mouse",   "event": N, "x": 0-65535, "y": 0-65535},
#       ...
#   ]

SEND_FILE = 'sharpbluesend.py'
_send_lock = threading.Lock()


def _load_pending():
    '''Read the current pending list from sharpbluesend.py, or return [].'''
    if not os.path.exists(SEND_FILE):
        return []
    try:
        ns = {}
        with open(SEND_FILE, 'r') as f:
            exec(f.read(), ns)
        return ns.get('pending', [])
    except Exception:
        return []


def _flush_pending(entries):
    '''Overwrite sharpbluesend.py with the given list of command dicts.'''
    lines = ['# sharpbluesend.py — BLE command queue (auto-generated by ble_gamepad.py)\n',
             '# Each entry is one command that would have been sent over BLE.\n',
             '# Consume entries from the top; ble_gamepad.py appends to the bottom.\n\n',
             'pending = [\n']
    for e in entries:
        lines.append(f'    {json.dumps(e)},\n')
    lines.append(']\n')
    with open(SEND_FILE, 'w') as f:
        f.writelines(lines)


def _save_command(cmd: dict):
    '''
    Append one BLE command to sharpbluesend.py instead of transmitting it.
    Thread-safe — called from the main thread and background timer threads.
    '''
    cmd['ts'] = datetime.datetime.now().isoformat(timespec='milliseconds')
    with _send_lock:
        pending = _load_pending()
        pending.append(cmd)
        _flush_pending(pending)
    print(f'[SEND→FILE] {cmd}')


# ── Dolphin DSU (CemuHook) Wi-Fi server ──────────────────────────────────────
#
# Implements the DSU/CemuHook protocol so Dolphin can read this device as a
# gamepad over Wi-Fi without any Bluetooth pairing.
#
# Setup in Dolphin:
#   Controllers → GameCube/Wii → DSU Client
#   Set IP = <this device's Wi-Fi IP>  Port = 26760
#   Slot 1 → Standard Controller / DSU Client
#
# Protocol reference: https://v1993.github.io/cemuhook-protocol/

class DolphinDSU:

    MAGIC_SERVER = b'DSUS'
    MAGIC_CLIENT = b'DSUC'
    PROTO_VER    = 1001
    PORT         = 26760
    SERVER_ID    = 0xDEADBEEF
    MAC          = bytes([0xAA, 0xBB, 0xCC, 0x00, 0x11, 0x22])

    MSG_VERSION  = 0x100001
    MSG_PORTS    = 0x100002
    MSG_DATA     = 0x100003

    def __init__(self, on_status):
        self.on_status   = on_status
        self._sock       = None
        self._running    = False
        self._thread     = None
        self._pkt_num    = 0
        self._clients    = {}   # addr -> last_request_time

    # ── Packet helpers ────────────────────────────────────────────────────────

    @staticmethod
    def _crc(data: bytes) -> int:
        return zlib.crc32(data) & 0xFFFFFFFF

    def _make_header(self, msg_type: int, payload: bytes) -> bytes:
        data_len = len(payload) + 4       # +4 for msg_type uint32
        hdr = struct.pack('<4sHHI',
            self.MAGIC_SERVER,
            self.PROTO_VER,
            data_len,
            0,                            # CRC placeholder
        ) + struct.pack('<I', self.SERVER_ID)
        body = hdr + struct.pack('<I', msg_type) + payload
        crc_val = self._crc(body)
        return body[:8] + struct.pack('<I', crc_val) + body[12:]

    def _send(self, addr, msg_type: int, payload: bytes):
        pkt = self._make_header(msg_type, payload)
        try:
            self._sock.sendto(pkt, addr)
        except Exception as e:
            print(f'[DSU] send error: {e}')

    # ── Response builders ─────────────────────────────────────────────────────

    def _resp_version(self, addr):
        self._send(addr, self.MSG_VERSION, struct.pack('<H', self.PROTO_VER))

    def _resp_ports(self, addr, slots):
        for slot in slots:
            payload = struct.pack('4B6sB',
                slot,           # slot index
                2,              # slot state: connected
                2,              # device model: full gyro
                2,              # connection: Bluetooth
                self.MAC,
                5,              # battery: full
            )
            self._send(addr, self.MSG_PORTS, payload)

    def _resp_data(self, addr, gp_state):
        self._pkt_num += 1
        s = gp_state

        hat = s['hat']
        dpad_u = 1 if hat in (0, 1, 7) else 0
        dpad_r = 1 if hat in (2, 1, 3) else 0
        dpad_d = 1 if hat in (4, 3, 5) else 0
        dpad_l = 1 if hat in (6, 5, 7) else 0

        btns = s['btns']

        byte1 = (dpad_l       |
                 dpad_d  << 1 |
                 dpad_r  << 2 |
                 dpad_u  << 3 |
                 (1 if btns[9]  else 0) << 4 |
                 (1 if btns[11] else 0) << 5 |
                 (1 if btns[10] else 0) << 6 |
                 (1 if btns[8]  else 0) << 7)

        byte2 = ((1 if btns[0] else 0)       |
                 (1 if btns[1] else 0) << 1  |
                 (1 if btns[2] else 0) << 2  |
                 (1 if btns[3] else 0) << 3  |
                 (1 if btns[5] else 0) << 4  |
                 (1 if btns[4] else 0) << 5  |
                 (1 if btns[7] else 0) << 6  |
                 (1 if btns[6] else 0) << 7)

        ps_btn    = 1 if btns[12] else 0
        touch_btn = 1 if btns[13] else 0

        lx = s['lx']
        ly = 255 - s['ly']
        rx = s['rx']
        ry = 255 - s['ry']
        l2 = s['l2']
        r2 = s['r2']

        timestamp = int(time.monotonic() * 1_000_000) & 0xFFFFFFFFFFFFFFFF

        payload = struct.pack('<B B B B 6s B B I',
            0, 2, 2, 2, self.MAC, 5, 1, self._pkt_num,
        )
        payload += struct.pack('10B',
            byte1, byte2,
            ps_btn, touch_btn,
            lx, ly, rx, ry,
            0, 0,
        )
        a_left  = 255 if dpad_l else 0
        a_down  = 255 if dpad_d else 0
        a_right = 255 if dpad_r else 0
        a_up    = 255 if dpad_u else 0
        a_sq = 255 if btns[0] else 0
        a_cr = 255 if btns[1] else 0
        a_ci = 255 if btns[2] else 0
        a_tr = 255 if btns[3] else 0
        a_r1 = 255 if btns[5] else 0
        a_l1 = 255 if btns[4] else 0

        payload += struct.pack('12B',
            a_left, a_down, a_right, a_up,
            a_sq, a_cr, a_ci, a_tr,
            a_r1, a_l1, r2, l2,
        )
        payload += bytes(12)
        payload += struct.pack('<Q', timestamp)
        payload += struct.pack('<6f', 0.0, 0.0, 1.0, 0.0, 0.0, 0.0)

        self._send(addr, self.MSG_DATA, payload)

    # ── Server loop ───────────────────────────────────────────────────────────

    def _loop(self):
        while self._running:
            try:
                data, addr = self._sock.recvfrom(1024)
            except socket.timeout:
                continue
            except Exception:
                break

            if len(data) < 20:
                continue
            magic = data[:4]
            if magic != self.MAGIC_CLIENT:
                continue

            msg_type = struct.unpack_from('<I', data, 16)[0]

            if msg_type == self.MSG_VERSION:
                self._resp_version(addr)

            elif msg_type == self.MSG_PORTS:
                count = struct.unpack_from('<I', data, 20)[0] if len(data) > 20 else 1
                slots = list(data[24:24 + count]) if len(data) > 24 else [0]
                self._resp_ports(addr, slots)

            elif msg_type == self.MSG_DATA:
                self._clients[addr] = time.monotonic()

    def send_state(self, gp_state):
        '''Called on every input change — pushes data to all subscribed clients.'''
        now  = time.monotonic()
        dead = [a for a, t in self._clients.items() if now - t > 5]
        for a in dead:
            del self._clients[a]
        for addr in list(self._clients):
            self._resp_data(addr, gp_state)

    # ── Start / stop ──────────────────────────────────────────────────────────

    def start(self):
        try:
            self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            self._sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            self._sock.bind(('0.0.0.0', self.PORT))
            self._sock.settimeout(0.5)
            self._running = True
            self._thread  = threading.Thread(target=self._loop, daemon=True)
            self._thread.start()
            ip = self._local_ip()
            self.on_status(f'Dolphin Wi-Fi ready  {ip}:{self.PORT}')
            print(f'[DSU] Server started on {ip}:{self.PORT}')
            return True
        except Exception as e:
            self.on_status(f'DSU error: {e}')
            print(f'[DSU] Start error: {e}')
            return False

    def stop(self):
        self._running = False
        if self._sock:
            self._sock.close()
        self.on_status('Dolphin Wi-Fi stopped')

    @staticmethod
    def _local_ip():
        try:
            s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            s.connect(('8.8.8.8', 80))
            ip = s.getsockname()[0]
            s.close()
            return ip
        except Exception:
            return '?.?.?.?'


def build_report():
    lx  = state['lx'];  ly  = state['ly']
    rx  = state['rx'];  ry  = state['ry']
    l2  = state['l2'];  r2  = state['r2']
    hat = state['hat'] & 0x0F
    btns = state['btns']

    btn_bits = 0
    for i, v in enumerate(btns):
        if v: btn_bits |= (1 << i)
    btn_lo = btn_bits & 0xFF
    btn_hi = (btn_bits >> 8) & 0x3F

    hat_byte = hat & 0x0F

    return struct.pack('9B',
        lx, ly, rx, ry,
        l2, r2,
        hat_byte,
        btn_lo,
        btn_hi,
    )


def send_mouse(event, nx, ny):
    '''
    Save a mouse event to sharpbluesend.py instead of transmitting over BLE.

    Packet format (6 bytes):
      [0]   event type  0=move  1=left_click  2=right_click
                        3=left_down  4=left_up  5=scroll_up  6=scroll_down
      [1]   reserved (0)
      [2-3] x  normalised 0-65535  (0=left  65535=right)
      [4-5] y  normalised 0-65535  (0=top   65535=bottom)
    '''
    xi = int(max(0.0, min(1.0, nx)) * 65535)
    yi = int(max(0.0, min(1.0, ny)) * 65535)

    event_names = {0:'move', 1:'left_click', 2:'right_click',
                   3:'left_down', 4:'left_up', 5:'scroll_up', 6:'scroll_down'}

    # ── Saved to sharpbluesend.py instead of BLE transmission ────────────────
    _save_command({
        'type':       'mouse',
        'event':      event,
        'event_name': event_names.get(event, 'unknown'),
        'x':          xi,
        'y':          yi,
    })


def send_report():
    data = build_report()
    report_bytes = list(data)

    # ── BLE HID path → saved to sharpbluesend.py instead of transmitting ─────
    _save_command({
        'type':   'gamepad',
        'report': report_bytes,
        'state': {
            'lx':   state['lx'], 'ly':   state['ly'],
            'rx':   state['rx'], 'ry':   state['ry'],
            'l2':   state['l2'], 'r2':   state['r2'],
            'hat':  state['hat'],
            'btns': list(state['btns']),
        },
    })

    # ── Dolphin DSU Wi-Fi path (still transmitted as before) ──────────────────
    if dolphin_dsu[0] is not None:
        dolphin_dsu[0].send_state(state)


# ── Peripheral manager delegate ───────────────────────────────────────────────

PROP_READ     = 0x02
PROP_WRITE_NR = 0x04
PROP_NOTIFY   = 0x10
PERM_READ     = 0x01
PERM_WRITE    = 0x02


def peripheralManagerDidUpdateState_(_self, _cmd, mgr):
    m = ObjCInstance(mgr)
    s = m.state()
    labels = {0:'Unknown',1:'Resetting',2:'Unsupported',
               3:'Unauthorized',4:'Off',5:'On'}
    print(f'[BLE] State: {labels.get(s, s)}')
    if status_cb[0]: status_cb[0](f'Bluetooth: {labels.get(s, s)}')
    if s == 5: _setup_services(m)


def peripheralManagerDidStartAdvertising_error_(_self, _cmd, mgr, err):
    if err:
        e = ObjCInstance(err)
        print(f'[BLE] Advertising error: {e.localizedDescription()}')
        if status_cb[0]: status_cb[0]('Advertising failed')
    else:
        print('[BLE] Advertising as "PyGamepad" — pair from Bluetooth settings')
        if status_cb[0]: status_cb[0]('Advertising — pair "PyGamepad" in BT settings')


def peripheralManager_didAddService_error_(_self, _cmd, mgr, svc, err):
    s = ObjCInstance(svc)
    print(f'[BLE] Added service {s.UUID().UUIDString()}' +
          ('' if not err else f'  ERROR: {ObjCInstance(err).localizedDescription()}'))


def peripheralManager_central_didSubscribeToCharacteristic_(
        _self, _cmd, mgr, central, char):
    subscribed[0] = True
    print('[BLE] Host subscribed — gamepad ready!')
    if status_cb[0]: status_cb[0]('Connected — gamepad ready!')


def peripheralManager_central_didUnsubscribeFromCharacteristic_(
        _self, _cmd, mgr, central, char):
    subscribed[0] = False
    print('[BLE] Host unsubscribed.')
    if status_cb[0]: status_cb[0]('Host disconnected')


def peripheralManager_didReceiveReadRequest_(_self, _cmd, mgr, request):
    m   = ObjCInstance(mgr)
    req = ObjCInstance(request)
    u   = str(req.characteristic().UUID().UUIDString()).upper()
    vals = {
        '2A4A': bytes([0x01, 0x01, 0x00, 0x02]),
        '2A4B': HID_DESCRIPTOR,
        '2A4E': bytes([0x01]),
        '2A19': bytes([0x64]),
        '2A29': b'Pythonista',
        '2A24': b'PyGamepad 1.0',
        '2A4D': bytes(9),
    }
    req.setValue_(nsdata(vals.get(u, b'')))
    m.respondToRequest_withResult_(req, 0)


def peripheralManager_didReceiveWriteRequests_(_self, _cmd, mgr, reqs):
    m = ObjCInstance(mgr)
    rs = ObjCInstance(reqs)
    for i in range(rs.count()):
        m.respondToRequest_withResult_(rs.objectAtIndex_(i), 0)


PMDelegate = create_objc_class(
    'PyGPDelegate',
    methods=[
        peripheralManagerDidUpdateState_,
        peripheralManagerDidStartAdvertising_error_,
        peripheralManager_didAddService_error_,
        peripheralManager_central_didSubscribeToCharacteristic_,
        peripheralManager_central_didUnsubscribeFromCharacteristic_,
        peripheralManager_didReceiveReadRequest_,
        peripheralManager_didReceiveWriteRequests_,
    ],
    protocols=['CBPeripheralManagerDelegate']
)


def _mk_char(u, props, perms, value=None):
    v = nsdata(value) if value else None
    return CBMutableCharacteristic.alloc()\
        .initWithType_properties_value_permissions_(u, props, v, perms)


def _setup_services(mgr):
    hid_info  = _mk_char(U_HID_INFO,   PROP_READ,            PERM_READ,
                          bytes([0x01,0x01,0x00,0x02]))
    hid_ctrl  = _mk_char(U_HID_CTRL,   PROP_WRITE_NR,        PERM_WRITE)
    rpt_map   = _mk_char(U_REPORT_MAP, PROP_READ,            PERM_READ, HID_DESCRIPTOR)
    proto     = _mk_char(U_PROTO_MODE, PROP_READ|PROP_WRITE_NR,
                          PERM_READ|PERM_WRITE, bytes([0x01]))

    rpt = _mk_char(U_REPORT, PROP_READ|PROP_NOTIFY, PERM_READ, bytes(9))
    rpt_ref = CBMutableDescriptor.alloc().initWithType_value_(
        U_REPORT_REF, nsdata(bytes([0x01, 0x01])))
    rpt.setDescriptors_(NSArray.arrayWithObject_(rpt_ref))
    report_char[0] = rpt

    hid_svc = CBMutableService.alloc().initWithType_primary_(U_HID_SVC, True)
    hid_svc.setCharacteristics_(NSArray.arrayWithArray_(
        [hid_info, hid_ctrl, rpt_map, proto, rpt]))
    mgr.addService_(hid_svc)

    batt_lv  = _mk_char(U_BATT_LEVEL, PROP_READ|PROP_NOTIFY, PERM_READ, bytes([0x64]))
    batt_svc = CBMutableService.alloc().initWithType_primary_(U_BATT_SVC, True)
    batt_svc.setCharacteristics_(NSArray.arrayWithObject_(batt_lv))
    mgr.addService_(batt_svc)

    manuf    = _mk_char(U_MANUF, PROP_READ, PERM_READ, b'Pythonista')
    model    = _mk_char(U_MODEL, PROP_READ, PERM_READ, b'PyGamepad 1.0')
    di_svc   = CBMutableService.alloc().initWithType_primary_(U_DEVINFO, True)
    di_svc.setCharacteristics_(NSArray.arrayWithArray_([manuf, model]))
    mgr.addService_(di_svc)

    # ── Custom mouse service ──────────────────────────────────────────────────
    mouse_ch = _mk_char(U_MOUSE_CHAR,
                        PROP_READ | PROP_NOTIFY | PROP_WRITE_NR,
                        PERM_READ | PERM_WRITE,
                        bytes(6))
    mouse_svc = CBMutableService.alloc().initWithType_primary_(U_MOUSE_SVC, True)
    mouse_svc.setCharacteristics_(NSArray.arrayWithObject_(mouse_ch))
    mouse_char[0] = mouse_ch
    mgr.addService_(mouse_svc)

    mgr.startAdvertising_({
        'kCBAdvDataLocalName':    ns('PyGamepad'),
        'kCBAdvDataServiceUUIDs': NSArray.arrayWithArray_(
            [U_HID_SVC, U_MOUSE_SVC]),
    })


# ── Joystick component ────────────────────────────────────────────────────────

class Joystick(ui.View):
    '''Circular virtual joystick that reports normalised x/y (-1..1).'''

    def __init__(self, callback, **kwargs):
        super().__init__(**kwargs)
        self.callback    = callback
        self.touch_x     = 0.5
        self.touch_y     = 0.5
        self.active      = False
        self.background_color = '#222222'
        self.corner_radius    = self.width / 2 if self.width else 60

    def draw(self):
        w, h = self.width, self.height
        r    = min(w, h) / 2
        cx, cy = w / 2, h / 2

        # Outer ring
        ui.set_color('#444444')
        path = ui.Path.oval(cx - r + 2, cy - r + 2, (r - 2) * 2, (r - 2) * 2)
        path.line_width = 2
        path.stroke()

        # Crosshair
        ui.set_color('#333333')
        ui.Path.line(cx - r + 10, cy, cx + r - 10, cy).stroke()
        ui.Path.line(cx, cy - r + 10, cx, cy + r - 10).stroke()

        # Thumb
        tx = cx + (self.touch_x - 0.5) * 2 * (r - 20)
        ty = cy + (self.touch_y - 0.5) * 2 * (r - 20)
        thumb_r = 18
        ui.set_color('#5588cc' if self.active else '#446699')
        p = ui.Path.oval(tx - thumb_r, ty - thumb_r, thumb_r * 2, thumb_r * 2)
        p.fill()

    def _clamp(self, v): return max(0.0, min(1.0, v))

    def touch_began(self, touch):
        self.active = True
        self._update(touch)

    def touch_moved(self, touch):
        self._update(touch)

    def touch_ended(self, touch):
        self.touch_x = 0.5
        self.touch_y = 0.5
        self.active  = False
        self.set_needs_display()
        self.callback(0.0, 0.0)

    def _update(self, touch):
        w, h  = self.width, self.height
        r     = min(w, h) / 2 - 20
        loc   = touch.location
        dx    = (loc[0] - w / 2) / r
        dy    = (loc[1] - h / 2) / r
        dist  = math.sqrt(dx * dx + dy * dy)
        if dist > 1.0:
            dx /= dist; dy /= dist
        self.touch_x = self._clamp((dx + 1) / 2)
        self.touch_y = self._clamp((dy + 1) / 2)
        self.set_needs_display()
        self.callback(dx, dy)


# ── Trigger slider ────────────────────────────────────────────────────────────

class TriggerSlider(ui.View):
    def __init__(self, label, callback, **kwargs):
        super().__init__(**kwargs)
        self.label_text  = label
        self.callback    = callback
        self.value       = 0.0
        self.background_color = '#222222'
        self.corner_radius    = 8

    def draw(self):
        w, h = self.width, self.height
        bar_h = h - 24
        filled = bar_h * self.value

        ui.set_color('#333333')
        ui.Path.rect(6, 12, w - 12, bar_h).fill()

        ui.set_color('#cc4444' if self.value > 0.5 else '#884444')
        ui.Path.rect(6, 12 + bar_h - filled, w - 12, filled).fill()

    def touch_began(self, touch): self._update(touch)
    def touch_moved(self, touch): self._update(touch)
    def touch_ended(self, touch):
        self.value = 0.0
        self.set_needs_display()
        self.callback(0)

    def _update(self, touch):
        bar_h  = self.height - 24
        loc    = touch.location
        ratio  = 1.0 - max(0.0, min(1.0, (loc[1] - 12) / bar_h))
        self.value = ratio
        self.set_needs_display()
        self.callback(int(ratio * 255))


# ── Remote Touchpad view ──────────────────────────────────────────────────────

class RemotePad(ui.View):
    '''
    Full-screen touchpad panel. Every touch/drag saves a mouse command to
    sharpbluesend.py instead of sending over BLE.

    Gestures:
      Single finger move  → move mouse (relative to touch start)
      Single tap          → left click
      Two-finger tap      → right click
      Two-finger swipe up/down → scroll
      Long-press button   → left button hold (drag)
    '''

    EVT_MOVE        = 0
    EVT_LEFT_CLICK  = 1
    EVT_RIGHT_CLICK = 2
    EVT_LEFT_DOWN   = 3
    EVT_LEFT_UP     = 4
    EVT_SCROLL_UP   = 5
    EVT_SCROLL_DOWN = 6

    def __init__(self, parent_ui, **kwargs):
        super().__init__(**kwargs)
        W, H = ui.get_screen_size()
        self.frame            = (0, 0, W, H)
        self.background_color = '#0d0d1a'
        self._parent_ui       = parent_ui
        self._last_touch      = None
        self._dragging        = False
        self._touch_count     = 0
        self._tap_timer       = None
        self._build()

    def _build(self):
        W, H = self.width or ui.get_screen_size()[0], self.height or ui.get_screen_size()[1]

        title = ui.Label(frame=(0, 18, W, 28))
        title.text            = '🖱  Remote Touchpad'
        title.text_color      = '#aaaacc'
        title.font            = ('<system-bold>', 14)
        title.alignment       = ui.ALIGN_CENTER
        self.add_subview(title)

        hint = ui.Label(frame=(0, 48, W, 20))
        hint.text       = 'drag=move  •  tap=click  •  2-finger tap=right-click  •  2-finger swipe=scroll'
        hint.text_color = '#444466'
        hint.font       = ('<system>', 10)
        hint.alignment  = ui.ALIGN_CENTER
        self.add_subview(hint)

        pad  = 20
        zone = ui.View(frame=(pad, 76, W - pad * 2, H - 76 - 90))
        zone.background_color = '#111122'
        zone.corner_radius    = 16
        zone.border_width     = 1
        zone.border_color     = '#223355'
        self.add_subview(zone)
        self._zone = zone

        zlbl = ui.Label(frame=(0, zone.height / 2 - 10, zone.width, 20))
        zlbl.text       = 'touch area'
        zlbl.text_color = '#1a1a33'
        zlbl.font       = ('<system>', 13)
        zlbl.alignment  = ui.ALIGN_CENTER
        zone.add_subview(zlbl)

        by  = H - 78
        bh  = 54
        bw  = (W - pad * 2 - 12) / 2
        lft = self._mk_btn('Left Click',  '#1a3a5c', bw, bh,
                           lambda s: self._do_click(self.EVT_LEFT_CLICK))
        lft.frame = (pad, by, bw, bh)
        self.add_subview(lft)

        rgt = self._mk_btn('Right Click', '#3a1a1a', bw, bh,
                           lambda s: self._do_click(self.EVT_RIGHT_CLICK))
        rgt.frame = (pad + bw + 12, by, bw, bh)
        self.add_subview(rgt)

        self._drag_btn = self._mk_btn('Hold / Drag', '#1a2a1a', 120, 34,
                                      self._toggle_drag)
        self._drag_btn.frame = (W / 2 - 60, by - 44, 120, 34)
        self.add_subview(self._drag_btn)

        self._status = ui.Label(frame=(0, by - 20, W, 18))
        self._status.text       = ''
        self._status.text_color = '#55aaff'
        self._status.font       = ('<system>', 11)
        self._status.alignment  = ui.ALIGN_CENTER
        self.add_subview(self._status)

    def _mk_btn(self, title, color, w, h, action):
        b = ui.Button(frame=(0, 0, w, h))
        b.title            = title
        b.background_color = color
        b.tint_color       = 'white'
        b.font             = ('<system-bold>', 13)
        b.corner_radius    = 10
        b.action           = action
        return b

    # ── Touch handling ────────────────────────────────────────────────────────

    def touch_began(self, touch):
        self._touch_count += 1
        loc = touch.location
        self._last_touch = loc
        self._status.text = ''

    def touch_moved(self, touch):
        if self._touch_count >= 2:
            return
        loc  = touch.location
        last = self._last_touch
        if last is None:
            self._last_touch = loc
            return
        W, H = self.width, self.height
        dx = (loc[0] - last[0]) / W
        dy = (loc[1] - last[1]) / H
        send_mouse(self.EVT_MOVE, 0.5 + dx, 0.5 + dy)
        self._last_touch = loc

    def touch_ended(self, touch):
        self._touch_count = max(0, self._touch_count - 1)
        if self._touch_count == 0:
            self._last_touch = None

    def touch_cancelled(self, touch):
        self._touch_count = 0
        self._last_touch  = None

    # ── Button actions ────────────────────────────────────────────────────────

    def _do_click(self, evt):
        name = 'Left click' if evt == self.EVT_LEFT_CLICK else 'Right click'
        send_mouse(evt, 0.5, 0.5)
        self._status.text = f'{name} saved → sharpbluesend.py'

    def _toggle_drag(self, sender):
        self._dragging = not self._dragging
        if self._dragging:
            send_mouse(self.EVT_LEFT_DOWN, 0.5, 0.5)
            self._drag_btn.background_color = '#4a7a4a'
            self._drag_btn.title = '⏹ Release Drag'
            self._status.text = 'Left button held — move finger to drag'
        else:
            send_mouse(self.EVT_LEFT_UP, 0.5, 0.5)
            self._drag_btn.background_color = '#1a2a1a'
            self._drag_btn.title = 'Hold / Drag'
            self._status.text = 'Drag released'


# ── Main UI ───────────────────────────────────────────────────────────────────

class GamepadUI(ui.View):

    BTN_DEFS = [
        ('□', 0, '#9966cc'), ('✕', 1, '#3377cc'),
        ('○', 2, '#cc4444'), ('△', 3, '#33aa66'),
        ('L1', 4, '#555555'), ('R1', 5, '#555555'),
        ('L2\nbtn', 6, '#444444'), ('R2\nbtn', 7, '#444444'),
        ('Share', 8, '#333333'), ('Opt', 9, '#333333'),
        ('L3', 10, '#444444'), ('R3', 11, '#444444'),
        ('PS', 12, '#cc8800'), ('⬜', 13, '#555566'),
    ]

    DPAD = [
        ('↑', 0), ('↗', 1), ('→', 2), ('↘', 3),
        ('↓', 4), ('↙', 5), ('←', 6), ('↖', 7),
    ]

    def __init__(self):
        self.background_color = '#111111'
        self._build()

    def _build(self):
        W, H = ui.get_screen_size()
        self.frame = (0, 0, W, H)

        self.status_lbl = ui.Label(frame=(0, 0, W, 38))
        self.status_lbl.text             = 'Commands → sharpbluesend.py'
        self.status_lbl.text_color       = '#ffdd44'
        self.status_lbl.font             = ('<system>', 12)
        self.status_lbl.alignment        = ui.ALIGN_CENTER
        self.status_lbl.background_color = '#111111'
        self.add_subview(self.status_lbl)
        status_cb[0] = lambda t: setattr(self.status_lbl, 'text', t)

        top = 44
        mid = H / 2
        pad = 12

        # ── Left joystick ─────────────────────────────────────────────────────
        js_size = min(W * 0.28, 130)
        ljs = Joystick(self._ljs_cb, frame=(pad, mid - js_size / 2, js_size, js_size))
        ljs.corner_radius = js_size / 2
        self.add_subview(ljs)
        self._add_label('L', pad + js_size / 2, mid + js_size / 2 + 4, W)

        # ── Right joystick ────────────────────────────────────────────────────
        rjs_x = W - pad - js_size
        rjs = Joystick(self._rjs_cb, frame=(rjs_x, mid - js_size / 2, js_size, js_size))
        rjs.corner_radius = js_size / 2
        self.add_subview(rjs)
        self._add_label('R', rjs_x + js_size / 2, mid + js_size / 2 + 4, W)

        # ── L2 / R2 triggers ─────────────────────────────────────────────────
        trig_w = 44; trig_h = 90
        l2 = TriggerSlider('L2', self._l2_cb,
                            frame=(pad, top + 4, trig_w, trig_h))
        r2 = TriggerSlider('R2', self._r2_cb,
                            frame=(W - pad - trig_w, top + 4, trig_w, trig_h))
        self.add_subview(l2); self.add_subview(r2)
        self._add_label('L2', pad + trig_w / 2, top + trig_h + 6, W)
        self._add_label('R2', W - pad - trig_w / 2, top + trig_h + 6, W)

        # ── D-pad ─────────────────────────────────────────────────────────────
        dp_cx = pad + js_size + 60
        dp_cy = top + 70
        dp_r  = 28
        dirs  = [(0,-1),(1,-1),(1,0),(1,1),(0,1),(-1,1),(-1,0),(-1,-1)]
        for (lbl, idx), (dx, dy) in zip(self.DPAD, dirs):
            bx = dp_cx + dx * dp_r * 1.55 - 22
            by = dp_cy + dy * dp_r * 1.55 - 22
            b  = self._make_btn(lbl, 44, 44, '#444444',
                                lambda s, i=idx: self._dpad(i))
            b.frame = (bx, by, 44, 44)
            self.add_subview(b)
        bc = self._make_btn('·', 30, 30, '#333333', lambda s: self._dpad(8))
        bc.frame = (dp_cx - 15, dp_cy - 15, 30, 30)
        self.add_subview(bc)

        # ── Face buttons ──────────────────────────────────────────────────────
        fc_cx = W - pad - js_size - 60
        fc_cy = top + 70
        face  = [(0,-1,0),(1,0,1),(0,1,2),(-1,0,3)]
        face_info = [('△',3,'#33aa66'),('○',2,'#cc4444'),
                     ('✕',1,'#3377cc'),('□',0,'#9966cc')]
        for (dx, dy, order), (lbl, idx, col) in zip(face, face_info):
            bx = fc_cx + dx * 38 - 22
            by = fc_cy + dy * 38 - 22
            b  = self._make_btn(lbl, 44, 44, col,
                                lambda s, i=idx: self._btn_press(i))
            b.frame = (bx, by, 44, 44)
            self.add_subview(b)

        # ── L1 / R1 ───────────────────────────────────────────────────────────
        l1 = self._make_btn('L1', 70, 36, '#555555', lambda s: self._btn_press(4))
        l1.frame = (pad + trig_w + 8, top + 4, 70, 36)
        self.add_subview(l1)

        r1 = self._make_btn('R1', 70, 36, '#555555', lambda s: self._btn_press(5))
        r1.frame = (W - pad - trig_w - 78, top + 4, 70, 36)
        self.add_subview(r1)

        # ── Share / Options ───────────────────────────────────────────────────
        sh = self._make_btn('Share', 70, 32, '#333333', lambda s: self._btn_press(8))
        sh.frame = (W / 2 - 76, top + 8, 70, 32)
        self.add_subview(sh)

        op = self._make_btn('Options', 70, 32, '#333333', lambda s: self._btn_press(9))
        op.frame = (W / 2 + 6, top + 8, 70, 32)
        self.add_subview(op)

        # ── PS / Touchpad ─────────────────────────────────────────────────────
        ps = self._make_btn('PS', 50, 50, '#cc8800', lambda s: self._btn_press(12))
        ps.corner_radius = 25
        ps.frame = (W / 2 - 25, mid - 25, 50, 50)
        self.add_subview(ps)

        tp = self._make_btn('Touch\npad', 80, 44, '#334455', lambda s: self._btn_press(13))
        tp.frame = (W / 2 - 40, mid + 36, 80, 44)
        self.add_subview(tp)

        self._add_label('L3=tap stick', dp_cx, top + 118, W, size=10)
        self._add_label('R3=tap stick', fc_cx, top + 118, W, size=10)

        # ── Remote Touchpad button ────────────────────────────────────────────
        rtp_btn = self._make_btn('🖱 Remote Pad', 130, 32,
                                 '#2a2a4a', self._open_remote_pad)
        rtp_btn.font  = ('<system>', 11)
        rtp_btn.frame = (W / 2 - 145, H - 48, 130, 32)
        self.add_subview(rtp_btn)

        # ── Dolphin Wi-Fi toggle ───────────────────────────────────────────────
        self._dolphin_active = False
        self._dolphin_btn = self._make_btn('🐬 Dolphin Wi-Fi', 140, 32,
                                           '#1a3a5c', self._toggle_dolphin)
        self._dolphin_btn.font = ('<system>', 11)
        self._dolphin_btn.frame = (W / 2 - 70, H - 48, 140, 32)
        self.add_subview(self._dolphin_btn)

        self._ip_lbl = ui.Label(frame=(0, H - 14, W, 14))
        self._ip_lbl.text             = ''
        self._ip_lbl.text_color       = '#55aaff'
        self._ip_lbl.font             = ('<system>', 10)
        self._ip_lbl.alignment        = ui.ALIGN_CENTER
        self._ip_lbl.background_color = '#111111'
        self.add_subview(self._ip_lbl)

    def _open_remote_pad(self, sender):
        pad = RemotePad(self)
        pad.present('sheet')

    def _toggle_dolphin(self, sender):
        if not self._dolphin_active:
            self._start_dolphin()
        else:
            self._stop_dolphin()

    def _start_dolphin(self):
        def _status(msg):
            self.status_lbl.text = msg
            if 'ready' in msg or 'Wi-Fi' in msg.lower():
                self._ip_lbl.text = msg
        dsu = DolphinDSU(on_status=_status)
        if dsu.start():
            dolphin_dsu[0]        = dsu
            self._dolphin_active  = True
            self._dolphin_btn.background_color = '#0d6e3e'
            self._dolphin_btn.title = '🐬 Dolphin  ON'
        else:
            self._ip_lbl.text = 'Failed to start DSU server'

    def _stop_dolphin(self):
        if dolphin_dsu[0]:
            dolphin_dsu[0].stop()
            dolphin_dsu[0] = None
        self._dolphin_active = False
        self._dolphin_btn.background_color = '#1a3a5c'
        self._dolphin_btn.title = '🐬 Dolphin Wi-Fi'
        self._ip_lbl.text = ''
        self.status_lbl.text = 'Dolphin Wi-Fi stopped'

    def _add_label(self, text, cx, y, W, size=11):
        lbl = ui.Label(frame=(cx - 50, y, 100, 16))
        lbl.text       = text
        lbl.text_color = '#888888'
        lbl.font       = ('<system>', size)
        lbl.alignment  = ui.ALIGN_CENTER
        self.add_subview(lbl)

    def _make_btn(self, title, w, h, color, action):
        b = ui.Button(frame=(0, 0, w, h))
        b.title            = title
        b.font             = ('<system-bold>', 13)
        b.tint_color       = 'white'
        b.background_color = color
        b.corner_radius    = 8
        b.action           = action
        return b

    # ── Input callbacks — all call send_report() which writes to sharpbluesend.py

    def _ljs_cb(self, x, y):
        state['lx'] = int((x + 1) / 2 * 255)
        state['ly'] = int((y + 1) / 2 * 255)
        send_report()

    def _rjs_cb(self, x, y):
        state['rx'] = int((x + 1) / 2 * 255)
        state['ry'] = int((y + 1) / 2 * 255)
        send_report()

    def _l2_cb(self, v):
        state['l2'] = v; send_report()

    def _r2_cb(self, v):
        state['r2'] = v; send_report()

    def _dpad(self, direction):
        state['hat'] = direction
        send_report()
        if direction != 8:
            threading.Timer(0.1, lambda: (state.update({'hat': 8}), send_report())).start()

    def _btn_press(self, idx):
        state['btns'][idx] = True
        send_report()
        threading.Timer(0.12, lambda: (state['btns'].__setitem__(idx, False), send_report())).start()


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    console.clear()
    print('BLE HID Gamepad — Pythonista')
    print('=' * 32)
    print('BLE transmission DISABLED.')
    print(f'All commands will be written to: {SEND_FILE}')
    print()

    # Initialise sharpbluesend.py with an empty queue on each launch
    _flush_pending([])
    print(f'[SEND] {SEND_FILE} cleared and ready.')

    view = GamepadUI()
    view.present('fullscreen')


if __name__ == '__main__':
    main()
