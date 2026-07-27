#!/usr/bin/env python3
"""
Barcode Generator — generates PDF417, Code 128, or Code 39 barcodes randomly
across multiple CPU cores and saves images when a match is found.

Deduplication: every code ever tried is remembered in seen_codes.txt so the
same code is never checked twice, even across restarts or target changes.
Memory usage and dedup can be controlled in config.py.

Usage:
  python barcode_generator.py
  Then answer the prompts: barcode type, match mode, number of cores.
"""

import random
import string
import io
import sys
import time
import os
from pathlib import Path
from multiprocessing import Process, Queue, Value, cpu_count
import ctypes

# ── File paths ────────────────────────────────────────────────────────────────
BASE          = Path(__file__).parent
EXIT_FILE     = BASE / "exit.py"
LOG_FILE      = BASE / "log.py"
SEEN_FILE     = BASE / "seen_codes.txt"
CONFIG_FILE   = BASE / "config.py"

# ── Module-level seen set (populated before forking; workers inherit via fork) ─
_SEEN: set[str] = set()


# ── Config loader ─────────────────────────────────────────────────────────────
def load_config() -> dict:
    defaults = {"dedup_enabled": True, "memory_warn_mb": 150}
    try:
        ns: dict = {}
        exec(CONFIG_FILE.read_text(), ns)
        return {
            "dedup_enabled": bool(ns.get("dedup_enabled", defaults["dedup_enabled"])),
            "memory_warn_mb": int(ns.get("memory_warn_mb", defaults["memory_warn_mb"])),
        }
    except Exception:
        return defaults


# ── Seen-codes helpers ────────────────────────────────────────────────────────
def load_seen() -> set[str]:
    """Load seen_codes.txt into a set. Called once before workers are forked."""
    if not SEEN_FILE.exists():
        return set()
    try:
        lines = SEEN_FILE.read_text().splitlines()
        return set(filter(None, lines))
    except Exception as exc:
        print(f"  WARNING: could not read seen_codes.txt — {exc}")
        return set()


def flush_seen(seen: set[str]) -> None:
    """Write entire seen set back to disk."""
    try:
        SEEN_FILE.write_text("\n".join(seen) + ("\n" if seen else ""))
    except Exception as exc:
        print(f"  WARNING: could not write seen_codes.txt — {exc}")


def seen_memory_mb(seen: set[str], avg_code_len: int) -> float:
    """Rough MB estimate: ~(50 bytes overhead + code length) per entry."""
    return len(seen) * (50 + avg_code_len) / 1_048_576


# ── Exit file check ───────────────────────────────────────────────────────────
def exit_requested() -> bool:
    try:
        ns: dict = {}
        exec(EXIT_FILE.read_text(), ns)
        return str(ns.get("stop", "")).strip().lower() == "yes"
    except Exception:
        return False


# ── Match logger ──────────────────────────────────────────────────────────────
def log_match(
    barcode_type: str,
    match_kind: str,
    code: str,
    saved_path: str,
    attempts: int,
    elapsed: float,
    worker_id: int,
) -> None:
    import datetime
    ts = datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    entry = (
        f"    {{\n"
        f"        \"timestamp\"   : \"{ts}\",\n"
        f"        \"type\"        : \"{barcode_type}\",\n"
        f"        \"match_kind\"  : \"{match_kind}\",\n"
        f"        \"code\"        : \"{code}\",\n"
        f"        \"file\"        : \"{saved_path}\",\n"
        f"        \"attempts\"    : {attempts},\n"
        f"        \"elapsed_sec\" : {elapsed:.2f},\n"
        f"        \"worker\"      : {worker_id},\n"
        f"    }},\n"
    )
    try:
        text = LOG_FILE.read_text()
        close = text.rfind("]")
        if close == -1:
            text = (
                "# Match log — automatically updated by barcode_generator.py\n\n"
                "matches = [\n" + entry + "]\n"
            )
        else:
            text = text[:close] + entry + text[close:]
        LOG_FILE.write_text(text)
    except Exception as exc:
        print(f"  WARNING: could not write to log.py — {exc}")


# ── Target text ───────────────────────────────────────────────────────────────
def load_target() -> str:
    target_file = BASE / "lookingForText.py"
    if not target_file.exists():
        print(f"ERROR: {target_file} not found.")
        sys.exit(1)
    namespace: dict = {}
    exec(target_file.read_text(), namespace)
    target = namespace.get("target", "").strip()
    if not target:
        print("ERROR: 'target' variable is empty in lookingForText.py")
        sys.exit(1)
    return target


# ── Character sets ────────────────────────────────────────────────────────────
CODE39_CHARS = string.ascii_uppercase + string.digits + "-. $/+%"
ALNUM_UPPER  = string.ascii_uppercase + string.digits


# ── Barcode image makers ──────────────────────────────────────────────────────
def make_code128(text: str):
    from barcode import Code128
    from barcode.writer import ImageWriter
    from PIL import Image
    bc = Code128(text, writer=ImageWriter())
    buf = io.BytesIO()
    bc.write(buf, options={"write_text": True})
    buf.seek(0)
    return Image.open(buf).copy()

def make_code39(text: str):
    from barcode import Code39
    from barcode.writer import ImageWriter
    from PIL import Image
    bc = Code39(text, writer=ImageWriter(), add_checksum=False)
    buf = io.BytesIO()
    bc.write(buf, options={"write_text": True})
    buf.seek(0)
    return Image.open(buf).copy()

def make_pdf417(text: str):
    import pdf417 as p417
    return p417.render_image(p417.encode(text, columns=2), scale=3, ratio=3)


# ── Save helpers ──────────────────────────────────────────────────────────────
def safe_name(text: str) -> str:
    return "".join(c if c.isalnum() else "_" for c in text)

def save_image(image, prefix: str, barcode_type: str, text: str, index: int = 0) -> str:
    out_dir = Path("found")
    out_dir.mkdir(exist_ok=True)
    if prefix == "MATCH":
        path = out_dir / f"MATCH_{barcode_type}_{safe_name(text)}.png"
    else:
        path = out_dir / f"PARTIAL_{barcode_type}_{index:04d}_{safe_name(text)}.png"
    image.save(str(path))
    return str(path)


# ── Worker process ────────────────────────────────────────────────────────────
def worker(
    worker_id: int,
    barcode_choice: str,
    target: str,
    gen_len: int,
    partial: bool,
    result_queue: Queue,
    codes_queue: Queue,    # sends batches of new codes back to main for seen tracking
    total_attempts: Value,
    stop_flag: Value,
    dedup_enabled: bool,
):
    """Runs in its own process. Inherits _SEEN via fork (Linux copy-on-write)."""
    random.seed(os.getpid() + worker_id * 999983)

    chars = CODE39_CHARS if barcode_choice == "39" else ALNUM_UPPER
    local_count  = 0
    new_codes: list[str] = []      # batch to send back for seen tracking
    REPORT_EVERY = 5_000
    CODES_BATCH  = 10_000          # send new codes to main every N codes

    while not stop_flag.value:
        code = "".join(random.choices(chars, k=gen_len))

        # ── Dedup check ───────────────────────────────────────────────────
        if dedup_enabled and code in _SEEN:
            continue   # skip already-tried code

        local_count += 1
        if dedup_enabled:
            new_codes.append(code)

        # Flush attempt counter
        if local_count % REPORT_EVERY == 0:
            with total_attempts.get_lock():
                total_attempts.value += REPORT_EVERY
            local_count = 0

        # Flush new codes batch to main process
        if dedup_enabled and len(new_codes) >= CODES_BATCH:
            codes_queue.put(new_codes)
            new_codes = []

        # ── Match check ───────────────────────────────────────────────────
        matched = (code == target) if not partial else (target in code)
        if matched:
            result_queue.put({"code": code, "worker": worker_id})
            if not partial:
                stop_flag.value = True
                break

    # Flush remaining
    if local_count:
        with total_attempts.get_lock():
            total_attempts.value += local_count
    if dedup_enabled and new_codes:
        codes_queue.put(new_codes)


# ── Main ──────────────────────────────────────────────────────────────────────
def main() -> None:
    global _SEEN
    import argparse

    parser = argparse.ArgumentParser(add_help=True)
    parser.add_argument("--type",   choices=["417","128","39"], help="Barcode type")
    parser.add_argument("--mode",   choices=["E","P","exact","partial"], help="Match mode")
    parser.add_argument("--cores",  type=int, help="Number of CPU cores")
    parser.add_argument("--target", type=str, help="Override the target in lookingForText.py")
    cli = parser.parse_args()

    print("=" * 58)
    print("  Barcode Generator — Multi-Core Match Finder")
    print("=" * 58)

    # 1. Barcode type
    if cli.type:
        choice = cli.type
        print(f"\nBarcode type : {choice}  (from --type)")
    else:
        while True:
            choice = input("\nBarcode type (417 / 128 / 39): ").strip()
            if choice in ("417", "128", "39"):
                break
            print("  Enter 417, 128, or 39.")

    # 2. Match mode
    if cli.mode:
        partial = cli.mode.upper() in ("P", "PARTIAL")
        print(f"Match mode   : {'partial' if partial else 'exact'}  (from --mode)")
    else:
        while True:
            mode = input("Match mode — exact (E) or partial (P)? ").strip().upper()
            if mode in ("E", "EXACT", "P", "PARTIAL"):
                partial = mode in ("P", "PARTIAL")
                break
            print("  Enter E or P.")

    # 3. Number of cores
    max_cores = cpu_count() or 1
    if cli.cores:
        num_cores = max(1, min(cli.cores, max_cores))
        print(f"Cores        : {num_cores}  (from --cores)")
    else:
        while True:
            raw = input(f"Number of cores to use (1–{max_cores}, default {max_cores}): ").strip()
            if raw == "":
                num_cores = max_cores
                break
            if raw.isdigit() and 1 <= int(raw) <= max_cores:
                num_cores = int(raw)
                break
            print(f"  Enter a number between 1 and {max_cores}.")

    # 4. Load config + target
    cfg = load_config()
    dedup = cfg["dedup_enabled"]
    warn_mb = cfg["memory_warn_mb"]

    if cli.target:
        target = cli.target.strip()
        print(f"Target       : {target!r}  (from --target)")
    else:
        target = load_target()
    target_len = len(target)
    gen_len    = target_len if not partial else max(target_len + 4, target_len * 2)

    # 5. Load seen codes BEFORE forking workers (they inherit via copy-on-write)
    skipped_at_start = 0
    if dedup:
        print(f"\n  Loading seen_codes.txt...", end=" ", flush=True)
        _SEEN = load_seen()
        skipped_at_start = len(_SEEN)
        print(f"{skipped_at_start:,} previously tried codes loaded.")

    type_label = "PDF417" if choice == "417" else f"Code {choice}"
    mode_label = "partial (target appears anywhere inside)" if partial else "exact"
    dedup_label = f"ON  ({skipped_at_start:,} codes already known)" if dedup else "OFF"

    print(f"\n{'─'*58}")
    print(f"  Barcode type  : {type_label}")
    print(f"  Match mode    : {mode_label}")
    print(f"  Target        : {target!r}  ({target_len} chars)")
    print(f"  Code length   : {gen_len} chars")
    print(f"  Cores         : {num_cores}")
    print(f"  Deduplication : {dedup_label}")
    print(f"{'─'*58}")
    print("  Searching...  Press Ctrl+C to stop.\n")

    # 6. Shared state
    result_queue   = Queue()
    codes_queue    = Queue()   # workers → main: batches of new codes
    total_attempts = Value(ctypes.c_longlong, 0)
    stop_flag      = Value(ctypes.c_bool, False)

    makers = {"417": make_pdf417, "128": make_code128, "39": make_code39}
    gen_img = makers[choice]
    label   = type_label.replace(" ", "")

    # 7. Spawn workers (fork inherits _SEEN)
    workers_list = []
    for wid in range(num_cores):
        p = Process(
            target=worker,
            args=(wid, choice, target, gen_len, partial,
                  result_queue, codes_queue, total_attempts, stop_flag, dedup),
            daemon=True,
        )
        p.start()
        workers_list.append(p)

    # 8. Main loop
    start          = time.time()
    partial_saves  = 0
    last_print     = start
    last_flush     = start
    last_cfg_check = start
    new_since_flush = 0          # codes added to _SEEN since last disk flush
    FLUSH_EVERY    = 50_000      # flush seen_codes.txt every N new codes
    warned_memory  = False

    try:
        while any(p.is_alive() for p in workers_list):
            time.sleep(0.25)

            # ── Drain new-codes queue → update _SEEN ─────────────────────
            if dedup:
                while not codes_queue.empty():
                    batch: list[str] = codes_queue.get_nowait()
                    _SEEN.update(batch)
                    new_since_flush += len(batch)

                # Flush to disk periodically
                if new_since_flush >= FLUSH_EVERY:
                    flush_seen(_SEEN)
                    new_since_flush = 0

            # ── Drain match queue ─────────────────────────────────────────
            while not result_queue.empty():
                hit    = result_queue.get_nowait()
                code   = hit["code"]
                elapsed = time.time() - start
                atts   = total_attempts.value

                if not partial:
                    stop_flag.value = True
                    print(f"\n{'='*58}")
                    print(f"  EXACT MATCH found by worker #{hit['worker']}!")
                    print(f"  Attempts : {atts:,}  ({elapsed:.1f}s, {num_cores} core(s))")
                    print(f"  Code     : {code}")
                    img  = gen_img(code)
                    path = save_image(img, "MATCH", label, code)
                    print(f"  Saved    : {path}")
                    log_match(label, "exact", code, path, atts, elapsed, hit["worker"])
                    print(f"  Logged   : log.py")
                    print(f"{'='*58}\n")
                else:
                    partial_saves += 1
                    pos = code.index(target)
                    print(f"\n  PARTIAL #{partial_saves}  worker #{hit['worker']}  "
                          f"attempt {atts:,}  ({elapsed:.1f}s)")
                    print(f"  Code : {code}  (target at position {pos})")
                    img  = gen_img(code)
                    path = save_image(img, "PARTIAL", label, code, partial_saves)
                    log_match(label, "partial", code, path, atts, elapsed, hit["worker"])
                    print(f"  Saved: {path}  →  logged to log.py\n")

            # ── Every 2 seconds: progress + exit check + memory check ─────
            now = time.time()
            if now - last_print >= 2.0:
                elapsed = now - start
                atts    = total_attempts.value
                rate    = atts / elapsed if elapsed > 0 else 0
                saves_info = f" | {partial_saves} save(s)" if partial else ""
                seen_info  = f" | seen {len(_SEEN):,}" if dedup else ""
                print(f"  [{atts:>12,} attempts | {rate:>10,.0f}/sec | "
                      f"{elapsed:>6.1f}s | {num_cores} core(s){saves_info}{seen_info}]")
                last_print = now

                # exit.py check
                if exit_requested():
                    stop_flag.value = True
                    print("\n  exit.py → stop = 'yes' — stopping all workers.")
                    break

                # Re-read config to detect dedup being turned off mid-run
                if now - last_cfg_check >= 10.0:
                    cfg = load_config()
                    last_cfg_check = now
                    if dedup and not cfg["dedup_enabled"]:
                        dedup = False
                        _SEEN.clear()
                        print("\n  config.py → dedup_enabled = False detected.")
                        print("  Seen set cleared — workers running at full speed.\n")

                # Memory warning (only when dedup is on)
                if dedup and not warned_memory:
                    used_mb = seen_memory_mb(_SEEN, gen_len)
                    warn_mb = cfg["memory_warn_mb"]
                    if used_mb >= warn_mb:
                        warned_memory = True
                        print(f"\n  ⚠  MEMORY WARNING: seen set is using ~{used_mb:.0f} MB "
                              f"({len(_SEEN):,} codes).")
                        print(f"     Threshold is {warn_mb} MB (set in config.py → memory_warn_mb).")
                        print(f"     To turn off deduplication and free this memory,")
                        print(f"     set  dedup_enabled = False  in config.py while running.\n")

            if stop_flag.value and not partial:
                break

    except KeyboardInterrupt:
        stop_flag.value = True
        print("\n  Stopping workers...")

    # 9. Final flush of seen codes
    if dedup and new_since_flush > 0:
        print(f"  Flushing {len(_SEEN):,} seen codes to seen_codes.txt...", end=" ")
        flush_seen(_SEEN)
        print("done.")

    # 10. Clean up workers
    for p in workers_list:
        p.join(timeout=2)
        if p.is_alive():
            p.terminate()

    elapsed = time.time() - start
    atts    = total_attempts.value
    rate    = atts / elapsed if elapsed > 0 else 0
    print(f"\n  Total : {atts:,} attempts in {elapsed:.1f}s "
          f"({rate:,.0f}/sec across {num_cores} core(s))")
    if dedup:
        print(f"  Seen  : {len(_SEEN):,} unique codes in seen_codes.txt")
    if partial:
        print(f"  Saves : {partial_saves} partial match(es)")


if __name__ == "__main__":
    main()
