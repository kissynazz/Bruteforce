# ── Deduplication settings ────────────────────────────────────────────────────
# Set dedup_enabled = False to skip the seen-codes check entirely.
# This frees all memory used by the seen set and lets workers run at max speed.
dedup_enabled = True

# Warn and offer to disable dedup when the seen set uses more than this many MB.
memory_warn_mb = 150
