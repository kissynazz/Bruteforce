#!/usr/bin/env python3
"""
clean.py — manage seen_codes.txt without touching your match history.

Usage:
  python clean.py
"""

from pathlib import Path

SEEN_FILE = Path(__file__).parent / "seen_codes.txt"


def load_seen() -> list[str]:
    if not SEEN_FILE.exists():
        return []
    return [c for c in SEEN_FILE.read_text().splitlines() if c.strip()]


def save_seen(codes: list[str]) -> None:
    SEEN_FILE.write_text("\n".join(codes) + ("\n" if codes else ""))


def fmt(n: int) -> str:
    return f"{n:,}"


def main() -> None:
    print("=" * 52)
    print("  seen_codes.txt — Clean Utility")
    print("=" * 52)

    codes = load_seen()
    total = len(codes)

    if total == 0:
        print("\n  seen_codes.txt is already empty. Nothing to do.")
        return

    # Estimate size
    avg_len = sum(len(c) for c in codes) / total if codes else 0
    est_mb  = total * (50 + avg_len) / 1_048_576

    print(f"\n  Codes in seen_codes.txt : {fmt(total)}")
    print(f"  Avg code length         : {avg_len:.1f} chars")
    print(f"  Estimated memory        : ~{est_mb:.1f} MB")

    print(f"""
  What would you like to do?

    1  Clear everything  — wipe all {fmt(total)} codes, start completely fresh
    2  Keep recent N     — remove the oldest codes, keep the most recent N
    3  Remove by prefix  — remove all codes starting with a specific string
    4  Show sample       — print a sample of stored codes
    0  Exit              — do nothing
""")

    while True:
        choice = input("  Choice: ").strip()
        if choice in ("0", "1", "2", "3", "4"):
            break
        print("  Enter 0, 1, 2, 3, or 4.")

    # ── Option 0: Exit ────────────────────────────────────────────────────────
    if choice == "0":
        print("\n  No changes made.")
        return

    # ── Option 1: Clear everything ────────────────────────────────────────────
    if choice == "1":
        confirm = input(f"\n  This will delete all {fmt(total)} codes. Type YES to confirm: ").strip()
        if confirm == "YES":
            save_seen([])
            print(f"  Done — seen_codes.txt cleared ({fmt(total)} codes removed).")
        else:
            print("  Cancelled.")

    # ── Option 2: Keep most recent N ─────────────────────────────────────────
    elif choice == "2":
        while True:
            raw = input(f"\n  How many recent codes to keep (1 – {fmt(total)}): ").strip()
            if raw.isdigit() and 1 <= int(raw) <= total:
                keep = int(raw)
                break
            print(f"  Enter a number between 1 and {fmt(total)}.")

        # Codes are stored in insertion order (oldest first)
        kept    = codes[-keep:]
        removed = total - keep
        save_seen(kept)
        print(f"  Done — removed {fmt(removed)} oldest codes, kept {fmt(keep)} most recent.")

    # ── Option 3: Remove by prefix ────────────────────────────────────────────
    elif choice == "3":
        prefix = input("\n  Remove codes starting with: ").strip()
        if not prefix:
            print("  Cancelled — empty prefix.")
            return
        kept    = [c for c in codes if not c.startswith(prefix)]
        removed = total - len(kept)
        if removed == 0:
            print(f"  No codes start with {prefix!r}. Nothing removed.")
        else:
            confirm = input(f"  Remove {fmt(removed)} code(s) starting with {prefix!r}? (Y/N): ").strip().upper()
            if confirm == "Y":
                save_seen(kept)
                print(f"  Done — {fmt(removed)} code(s) removed, {fmt(len(kept))} remaining.")
            else:
                print("  Cancelled.")

    # ── Option 4: Show sample ─────────────────────────────────────────────────
    elif choice == "4":
        sample_size = min(20, total)
        import random
        sample = random.sample(codes, sample_size)
        print(f"\n  Random sample of {sample_size} codes from seen_codes.txt:")
        for c in sorted(sample):
            print(f"    {c}")
        print()
        # After showing sample, offer to go back
        main()


if __name__ == "__main__":
    main()
