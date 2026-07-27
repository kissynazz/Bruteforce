//
// SharpBlue
//
// Background BLE relay for Nazzara's iPad.
//
// What it does:
//   1. Scans for "Nazzara's iPad" and connects to it via CoreBluetooth.
//   2. Discovers the device's services and writable characteristics.
//   3. Runs a background polling loop every 1 second that reads
//      sharpbluesend.py, writes every pending command over BLE to the
//      connected peripheral, then clears the sent entries from the file.
//
// Command types understood from sharpbluesend.py:
//   "gamepad" — 9-byte HID report written to the HID Report characteristic (0x2A4D)
//   "mouse"   — 6-byte packet written to the custom mouse characteristic
//
// Background execution:
//   Uses UIApplication.BeginBackgroundTask so the relay loop keeps running
//   even when the app is moved to the background.
//   iOS gives ~30 s of true background time; after that the loop re-requests
//   a task on each tick to stay alive as long as the OS allows.
//

using System;
using System.Collections.Generic;
using System.IO;
using System.Linq;
using System.Text;
using System.Text.RegularExpressions;
using System.Threading;
using System.Threading.Tasks;

using CoreBluetooth;
using CoreFoundation;
using Foundation;
using UIKit;

// ── Tunables ──────────────────────────────────────────────────────────────────

static class Cfg {
    public static readonly string TargetName   = "Nazzara's iPad";
    public static readonly string SendFile     = "sharpbluesend.py";
    public static readonly double PollInterval = 1.0;          // seconds

    // UUIDs that ble_gamepad.py publishes
    public static readonly CBUUID HidService    = CBUUID.FromString("1812");
    public static readonly CBUUID HidReport     = CBUUID.FromString("2A4D");   // gamepad report
    public static readonly CBUUID HidControl    = CBUUID.FromString("2A4C");   // control point
    public static readonly CBUUID MouseService  = CBUUID.FromString("A000B000-C000-D000-E000-F00000000001");
    public static readonly CBUUID MouseChar     = CBUUID.FromString("A000B000-C000-D000-E000-F00000000002");

    // UI colours
    public static readonly UIColor Accent  = UIColor.SystemBlueColor;
    public static readonly UIColor Good    = UIColor.SystemGreenColor;
    public static readonly UIColor Warn    = UIColor.SystemOrangeColor;
    public static readonly UIColor Danger  = UIColor.SystemRedColor;
}

// ── Parsed command from sharpbluesend.py ──────────────────────────────────────

class BleCommand {
    public string   Type;        // "gamepad" | "mouse"
    public string   Timestamp;
    public byte[]   Bytes;       // ready-to-write payload
    public string   Raw;         // original JSON line (for clearing)

    // Build the 9-byte gamepad HID report from the "report" int array
    public static BleCommand Gamepad(string ts, int[] report, string raw) {
        var bytes = new byte[report.Length];
        for (int i = 0; i < report.Length; i++) bytes[i] = (byte)(report[i] & 0xFF);
        return new BleCommand { Type = "gamepad", Timestamp = ts, Bytes = bytes, Raw = raw };
    }

    // Build the 6-byte mouse packet  [event, 0, xLo, xHi, yLo, yHi]
    public static BleCommand Mouse(string ts, int evt, int x, int y, string raw) {
        var bytes = new byte[6];
        bytes[0] = (byte)(evt & 0xFF);
        bytes[1] = 0;
        bytes[2] = (byte)(x & 0xFF);
        bytes[3] = (byte)((x >> 8) & 0xFF);
        bytes[4] = (byte)(y & 0xFF);
        bytes[5] = (byte)((y >> 8) & 0xFF);
        return new BleCommand { Type = "mouse", Timestamp = ts, Bytes = bytes, Raw = raw };
    }
}

// ── sharpbluesend.py parser ───────────────────────────────────────────────────
//
// The file looks like:
//   pending = [
//       {"ts": "...", "type": "gamepad", "report": [128,128,...], ...},
//       {"ts": "...", "type": "mouse",   "event": 0, "x": 32767, "y": 32767},
//   ]
//
// Each entry is a valid JSON object, so we split on lines and parse individually.

static class SendFileReader {

    static readonly Regex EntryLine = new Regex(
        @"^\s*\{.*""type""\s*:\s*""(?<t>gamepad|mouse)"".*\},?\s*$",
        RegexOptions.Compiled);

    public static List<BleCommand> Read(string path) {
        var result = new List<BleCommand>();
        if (!File.Exists(path)) return result;

        string text;
        try { text = File.ReadAllText(path); }
        catch { return result; }

        // Extract the array content between the first '[' and last ']'
        int start = text.IndexOf('[');
        int end   = text.LastIndexOf(']');
        if (start < 0 || end <= start) return result;

        string arrayText = text.Substring(start + 1, end - start - 1);
        foreach (var rawLine in arrayText.Split('\n')) {
            var line = rawLine.Trim().TrimEnd(',');
            if (!line.StartsWith("{") || !line.EndsWith("}")) continue;

            var cmd = ParseEntry(line);
            if (cmd != null) result.Add(cmd);
        }
        return result;
    }

    // Write back the file with only the entries NOT in sentRaw
    public static void ClearSent(string path, HashSet<string> sentRaw) {
        if (!File.Exists(path)) return;
        var remaining = Read(path).Where(c => !sentRaw.Contains(c.Raw)).ToList();

        var sb = new StringBuilder();
        sb.AppendLine("# sharpbluesend.py — BLE command queue (auto-generated by ble_gamepad.py)");
        sb.AppendLine("# Entries are removed after SharpBlue sends them.");
        sb.AppendLine();
        sb.AppendLine("pending = [");
        foreach (var c in remaining) sb.AppendLine($"    {c.Raw},");
        sb.AppendLine("]");

        try { File.WriteAllText(path, sb.ToString()); }
        catch { /* non-fatal */ }
    }

    static BleCommand ParseEntry(string json) {
        try {
            // Use NSJSONSerialization to parse the object
            var data = NSData.FromString(json, NSStringEncoding.UTF8);
            NSError err;
            var obj  = NSJsonSerialization.Deserialize(data, 0, out err) as NSDictionary;
            if (obj == null) return null;

            string ts   = obj["ts"]?.ToString()   ?? "";
            string type = obj["type"]?.ToString() ?? "";

            if (type == "gamepad") {
                var reportArr = obj["report"] as NSArray;
                if (reportArr == null) return null;
                var ints = new int[reportArr.Count];
                for (nuint i = 0; i < reportArr.Count; i++)
                    ints[i] = (int)(reportArr.GetItem<NSNumber>(i).Int32Value);
                return BleCommand.Gamepad(ts, ints, json);
            }

            if (type == "mouse") {
                int evt = ((NSNumber)obj["event"]).Int32Value;
                int x   = ((NSNumber)obj["x"]).Int32Value;
                int y   = ((NSNumber)obj["y"]).Int32Value;
                return BleCommand.Mouse(ts, evt, x, y, json);
            }
        }
        catch { /* skip malformed entries */ }
        return null;
    }
}

// ── Peripheral delegate — service & characteristic discovery ─────────────────

class DeviceDelegate : CBPeripheralDelegate {
    public CBCharacteristic HidReportChar;
    public CBCharacteristic MouseCharChar;
    public Action<string>   OnStatus;
    public Action<string>   OnReady;

    int _discovered;

    public override void DiscoveredService(CBPeripheral peripheral, NSError error) {
        if (error != null) { OnStatus?.Invoke($"Service error: {error.LocalizedDescription}"); return; }
        foreach (var svc in peripheral.Services ?? new CBService[0]) {
            if (svc.UUID.Equals(Cfg.HidService) || svc.UUID.Equals(Cfg.MouseService))
                peripheral.DiscoverCharacteristics(null, svc);
        }
    }

    public override void DiscoveredCharacteristic(CBPeripheral peripheral,
        CBService service, NSError error) {
        if (error != null) return;
        foreach (var ch in service.Characteristics ?? new CBCharacteristic[0]) {
            if (ch.UUID.Equals(Cfg.HidReport))  HidReportChar  = ch;
            if (ch.UUID.Equals(Cfg.MouseChar))  MouseCharChar  = ch;
        }
        _discovered++;
        // Both services discovered → ready
        if (_discovered >= 2)
            OnReady?.Invoke($"Ready — relaying commands from {Cfg.SendFile}");
    }

    public override void WroteCharacteristicValue(CBPeripheral peripheral,
        CBCharacteristic characteristic, NSError error) {
        // Silently ignore write confirmations; errors are rare for write-no-response
    }
}

// ── Background relay loop ─────────────────────────────────────────────────────

class Relay {
    readonly CBPeripheral  peripheral;
    readonly DeviceDelegate devDelegate;
    readonly Action<string> onStatus;
    readonly Action<int>    onCount;      // total commands sent so far

    CancellationTokenSource cts;
    int totalSent;
    nint bgTask = UIApplication.BackgroundTaskInvalid;

    public Relay(CBPeripheral p, DeviceDelegate d,
                 Action<string> status, Action<int> count) {
        peripheral  = p;
        devDelegate = d;
        onStatus    = status;
        onCount     = count;
    }

    public void Start() {
        cts = new CancellationTokenSource();
        Task.Run(() => Loop(cts.Token));
        onStatus("Relay started — watching " + Cfg.SendFile);
    }

    public void Stop() {
        cts?.Cancel();
        EndBgTask();
        onStatus("Relay stopped.");
    }

    void Loop(CancellationToken token) {
        while (!token.IsCancellationRequested) {
            // Re-acquire background task on every tick so iOS keeps renewing it
            BeginBgTask();

            var commands = SendFileReader.Read(Cfg.SendFile);
            if (commands.Count > 0) {
                var sent = new HashSet<string>();
                foreach (var cmd in commands) {
                    if (token.IsCancellationRequested) break;
                    bool ok = WriteCommand(cmd);
                    if (ok) { sent.Add(cmd.Raw); totalSent++; }
                }
                SendFileReader.ClearSent(Cfg.SendFile, sent);
                InvokeOnMain(() => {
                    onStatus($"Sent {commands.Count} cmd(s) — {totalSent} total");
                    onCount(totalSent);
                });
            }

            Thread.Sleep(TimeSpan.FromSeconds(Cfg.PollInterval));
        }
        EndBgTask();
    }

    bool WriteCommand(BleCommand cmd) {
        CBCharacteristic ch = null;

        if (cmd.Type == "gamepad") ch = devDelegate.HidReportChar;
        if (cmd.Type == "mouse")   ch = devDelegate.MouseCharChar;

        if (ch == null) return false;

        var data = NSData.FromArray(cmd.Bytes);
        peripheral.WriteValue(data, ch, CBCharacteristicWriteType.WithoutResponse);
        return true;
    }

    // ── Background task helpers ───────────────────────────────────────────────

    void BeginBgTask() {
        InvokeOnMain(() => {
            if (bgTask != UIApplication.BackgroundTaskInvalid) return;
            bgTask = UIApplication.SharedApplication.BeginBackgroundTask("SharpBlueRelay",
                () => { EndBgTask(); });   // expiration handler
        });
    }

    void EndBgTask() {
        InvokeOnMain(() => {
            if (bgTask == UIApplication.BackgroundTaskInvalid) return;
            UIApplication.SharedApplication.EndBackgroundTask(bgTask);
            bgTask = UIApplication.BackgroundTaskInvalid;
        });
    }

    static void InvokeOnMain(Action a) =>
        DispatchQueue.MainQueue.DispatchAsync(a);
}

// ── Central manager delegate ──────────────────────────────────────────────────

class BlueDelegate : CBCentralManagerDelegate {
    public Action<CBManagerState>                            OnState;
    public Action<CBPeripheral, NSDictionary, NSNumber>      OnDiscover;
    public Action<CBPeripheral>                              OnConnect;
    public Action<CBPeripheral, NSError>                     OnFail;
    public Action<CBPeripheral, NSError>                     OnDisconnect;

    public override void UpdatedState(CBCentralManager central)                                         => OnState?.Invoke(central.State);
    public override void DiscoveredPeripheral(CBCentralManager c, CBPeripheral p, NSDictionary ad, NSNumber r) => OnDiscover?.Invoke(p, ad, r);
    public override void ConnectedPeripheral(CBCentralManager c, CBPeripheral p)                         => OnConnect?.Invoke(p);
    public override void FailedToConnectPeripheral(CBCentralManager c, CBPeripheral p, NSError e)        => OnFail?.Invoke(p, e);
    public override void DisconnectedPeripheral(CBCentralManager c, CBPeripheral p, NSError e)           => OnDisconnect?.Invoke(p, e);
}

// ── Table source for discovered peripherals ───────────────────────────────────

class DeviceSource : UITableViewDataSource {
    readonly List<(CBPeripheral p, NSNumber rssi)> items;
    readonly string target;

    public DeviceSource(List<(CBPeripheral, NSNumber)> items, string target) {
        this.items  = items;
        this.target = target;
    }

    public override nint NumberOfSections(UITableView tv) => 1;
    public override nint RowsInSection(UITableView tv, nint s) => items.Count;

    public override UITableViewCell GetCell(UITableView tv, NSIndexPath ip) {
        var cell = tv.DequeueReusableCell("c") ??
                   new UITableViewCell(UITableViewCellStyle.Subtitle, "c");
        var (p, rssi) = items[ip.Row];
        bool isTarget = p.Name == target;
        cell.TextLabel.Text       = string.IsNullOrEmpty(p.Name) ? "(unnamed)" : p.Name;
        cell.DetailTextLabel.Text = $"RSSI {rssi} dBm  ·  {p.Identifier}";
        cell.TextLabel.TextColor  = isTarget ? Cfg.Good : UIColor.LabelColor;
        cell.TextLabel.Font       = isTarget
            ? UIFont.BoldSystemFontOfSize(16)
            : UIFont.SystemFontOfSize(16);
        return cell;
    }
}

// ── Main view controller ──────────────────────────────────────────────────────

class BlueController : UIViewController {

    // BLE
    CBCentralManager  central;
    BlueDelegate      btDelegate;
    CBPeripheral      target;
    DeviceDelegate    devDelegate;
    Relay             relay;

    // Data
    List<(CBPeripheral p, NSNumber rssi)> discovered = new();

    // UI
    UILabel      statusLabel;
    UILabel      counterLabel;
    UIButton     connectBtn;
    UIButton     relayBtn;
    UITableView  tableView;

    public BlueController() { Title = "SharpBlue"; }

    public override void ViewDidLoad() {
        base.ViewDidLoad();
        View.BackgroundColor = UIColor.SystemBackgroundColor;
        BuildUI();
        InitBluetooth();
    }

    // ── UI construction ───────────────────────────────────────────────────────

    void BuildUI() {
        // Status
        statusLabel = MakeLabel("Initialising Bluetooth…", UIFont.SystemFontOfSize(13),
                                UIColor.SecondaryLabelColor);

        // Counter (commands relayed)
        counterLabel = MakeLabel("Relayed: 0 commands", UIFont.SystemFontOfSize(12),
                                 UIColor.SystemBlueColor);

        // Connect button
        connectBtn = MakeButton($"Connect to {Cfg.TargetName}", Cfg.Good);
        connectBtn.Hidden = true;
        connectBtn.TouchUpInside += OnConnectTapped;

        // Relay toggle button
        relayBtn = MakeButton("▶  Start Relay", Cfg.Accent);
        relayBtn.Hidden = true;
        relayBtn.TouchUpInside += OnRelayTapped;

        // Table
        tableView = new UITableView {
            TranslatesAutoresizingMaskIntoConstraints = false
        };

        View.AddSubviews(statusLabel, counterLabel, connectBtn, relayBtn, tableView);

        var g = View.SafeAreaLayoutGuide;
        NSLayoutConstraint.ActivateConstraints(new[] {
            statusLabel.TopAnchor.ConstraintEqualTo(g.TopAnchor, 12),
            statusLabel.LeadingAnchor.ConstraintEqualTo(View.LeadingAnchor, 16),
            statusLabel.TrailingAnchor.ConstraintEqualTo(View.TrailingAnchor, -16),

            counterLabel.TopAnchor.ConstraintEqualTo(statusLabel.BottomAnchor, 4),
            counterLabel.LeadingAnchor.ConstraintEqualTo(View.LeadingAnchor, 16),
            counterLabel.TrailingAnchor.ConstraintEqualTo(View.TrailingAnchor, -16),

            connectBtn.TopAnchor.ConstraintEqualTo(counterLabel.BottomAnchor, 12),
            connectBtn.LeadingAnchor.ConstraintEqualTo(View.LeadingAnchor, 24),
            connectBtn.TrailingAnchor.ConstraintEqualTo(View.TrailingAnchor, -24),
            connectBtn.HeightAnchor.ConstraintEqualTo(48),

            relayBtn.TopAnchor.ConstraintEqualTo(connectBtn.BottomAnchor, 8),
            relayBtn.LeadingAnchor.ConstraintEqualTo(View.LeadingAnchor, 24),
            relayBtn.TrailingAnchor.ConstraintEqualTo(View.TrailingAnchor, -24),
            relayBtn.HeightAnchor.ConstraintEqualTo(48),

            tableView.TopAnchor.ConstraintEqualTo(relayBtn.BottomAnchor, 12),
            tableView.LeadingAnchor.ConstraintEqualTo(View.LeadingAnchor),
            tableView.TrailingAnchor.ConstraintEqualTo(View.TrailingAnchor),
            tableView.BottomAnchor.ConstraintEqualTo(View.BottomAnchor),
        });

        NavigationItem.RightBarButtonItem = new UIBarButtonItem(
            "Scan", UIBarButtonItemStyle.Plain, (s, e) => BeginScan());
        NavigationItem.LeftBarButtonItem = new UIBarButtonItem(
            "Stop", UIBarButtonItemStyle.Plain, (s, e) => StopScan());
    }

    UILabel MakeLabel(string text, UIFont font, UIColor color) {
        return new UILabel {
            Text          = text,
            Font          = font,
            TextColor     = color,
            TextAlignment = UITextAlignment.Center,
            Lines         = 0,
            TranslatesAutoresizingMaskIntoConstraints = false
        };
    }

    UIButton MakeButton(string title, UIColor bg) {
        var b = UIButton.FromType(UIButtonType.System);
        b.SetTitle(title, UIControlState.Normal);
        b.SetTitleColor(UIColor.WhiteColor, UIControlState.Normal);
        b.BackgroundColor = bg;
        b.Layer.CornerRadius = 12;
        b.TranslatesAutoresizingMaskIntoConstraints = false;
        return b;
    }

    // ── Bluetooth init ────────────────────────────────────────────────────────

    void InitBluetooth() {
        btDelegate = new BlueDelegate {
            OnState      = s  => InvokeOnMainThread(() => HandleState(s)),
            OnDiscover   = (p, ad, r) => InvokeOnMainThread(() => HandleDiscovered(p, r)),
            OnConnect    = p  => InvokeOnMainThread(() => HandleConnected(p)),
            OnFail       = (p, e) => InvokeOnMainThread(() => HandleFailed(p, e)),
            OnDisconnect = (p, e) => InvokeOnMainThread(() => HandleDisconnected(p, e)),
        };
        central = new CBCentralManager(btDelegate, DispatchQueue.MainQueue);
    }

    // ── State ─────────────────────────────────────────────────────────────────

    void HandleState(CBManagerState s) {
        switch (s) {
            case CBManagerState.PoweredOn:
                statusLabel.Text = "Bluetooth ready — scanning…";
                BeginScan();
                break;
            case CBManagerState.PoweredOff:
                statusLabel.Text = "Bluetooth off — enable in Settings.";
                break;
            case CBManagerState.Unauthorized:
                statusLabel.Text = "Bluetooth permission denied.";
                break;
            default:
                statusLabel.Text = $"Bluetooth: {s}";
                break;
        }
    }

    // ── Scanning ──────────────────────────────────────────────────────────────

    void BeginScan() {
        if (central.State != CBManagerState.PoweredOn) return;
        discovered.Clear();
        target = null;
        connectBtn.Hidden = true;
        relayBtn.Hidden   = true;
        RefreshTable();
        statusLabel.Text = $"Scanning for {Cfg.TargetName}…";
        central.ScanForPeripherals(peripheralUuids: null,
            options: new PeripheralScanningOptions { AllowDuplicatesKey = false });
    }

    void StopScan() {
        central.StopScan();
        statusLabel.Text = $"Scan stopped — {discovered.Count} device(s) found.";
    }

    void HandleDiscovered(CBPeripheral p, NSNumber rssi) {
        var existing = discovered.FindIndex(d => d.p.Identifier == p.Identifier);
        if (existing >= 0)
            discovered[existing] = (p, rssi);
        else
            discovered.Add((p, rssi));

        if (p.Name == Cfg.TargetName && target == null) {
            target = p;
            connectBtn.Hidden    = false;
            statusLabel.Text     = $"✓ {Cfg.TargetName} found!";
            statusLabel.TextColor = Cfg.Good;
        }
        RefreshTable();
    }

    // ── Connection ────────────────────────────────────────────────────────────

    void OnConnectTapped(object sender, EventArgs e) {
        if (target == null) return;
        StopScan();
        statusLabel.Text       = $"Connecting to {Cfg.TargetName}…";
        statusLabel.TextColor  = UIColor.SecondaryLabelColor;
        connectBtn.Enabled     = false;
        central.ConnectPeripheral(target, new PeripheralConnectionOptions());
    }

    void HandleConnected(CBPeripheral p) {
        statusLabel.Text  = $"Connected — discovering services…";
        statusLabel.TextColor = Cfg.Good;
        connectBtn.SetTitle("Connected ✓", UIControlState.Normal);
        connectBtn.BackgroundColor = Cfg.Accent;

        // Set up peripheral delegate for service discovery
        devDelegate = new DeviceDelegate {
            OnStatus = msg => InvokeOnMainThread(() => statusLabel.Text = msg),
            OnReady  = msg => InvokeOnMainThread(() => {
                statusLabel.Text = msg;
                relayBtn.Hidden  = false;
            }),
        };
        p.Delegate = devDelegate;
        // Discover only the services we care about
        p.DiscoverServices(new[] { Cfg.HidService, Cfg.MouseService });
    }

    void HandleFailed(CBPeripheral p, NSError e) {
        statusLabel.Text      = $"Connection failed: {e?.LocalizedDescription ?? "unknown"}";
        statusLabel.TextColor = Cfg.Danger;
        connectBtn.Enabled    = true;
    }

    void HandleDisconnected(CBPeripheral p, NSError e) {
        relay?.Stop();
        relay = null;
        statusLabel.Text       = $"Disconnected from {p.Name}. Tap Scan to retry.";
        statusLabel.TextColor  = Cfg.Warn;
        relayBtn.Hidden        = true;
        connectBtn.SetTitle($"Connect to {Cfg.TargetName}", UIControlState.Normal);
        connectBtn.BackgroundColor = Cfg.Good;
        connectBtn.Enabled    = true;
        connectBtn.Hidden     = target == null;
    }

    // ── Relay ─────────────────────────────────────────────────────────────────

    void OnRelayTapped(object sender, EventArgs e) {
        if (relay == null) {
            relay = new Relay(target, devDelegate,
                msg   => InvokeOnMainThread(() => statusLabel.Text = msg),
                count => InvokeOnMainThread(() =>
                    counterLabel.Text = $"Relayed: {count} command(s)"));
            relay.Start();
            relayBtn.SetTitle("⏹  Stop Relay", UIControlState.Normal);
            relayBtn.BackgroundColor = Cfg.Danger;
        } else {
            relay.Stop();
            relay = null;
            relayBtn.SetTitle("▶  Start Relay", UIControlState.Normal);
            relayBtn.BackgroundColor = Cfg.Accent;
        }
    }

    // ── Table ─────────────────────────────────────────────────────────────────

    void RefreshTable() {
        var sorted = discovered
            .OrderByDescending(d => d.p.Name == Cfg.TargetName)
            .ThenByDescending(d => d.rssi.Int32Value)
            .ToList();
        discovered = sorted;
        tableView.DataSource = new DeviceSource(discovered, Cfg.TargetName);
        tableView.ReloadData();
    }
}

// ── Entry point ───────────────────────────────────────────────────────────────

var Main = new UINavigationController(new BlueController());
