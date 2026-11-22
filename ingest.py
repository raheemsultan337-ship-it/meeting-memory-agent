"""Stage [1] PARSE + keyword INDEX: transcripts -> turns table + FTS5 index.

Pure code, no LLM: the transcript format is fixed, so parsing is deterministic.

Usage:  python ingest.py
"""
import re
import sqlite3
from pathlib import Path

import config
import db

# "00:01:05 Speaker B" -> groups ('00', '01', '05', 'B')
TURN_HEADER = re.compile(r"^(\d\d):(\d\d):(\d\d) Speaker ([A-Z])$")

# Spelled-out acronyms: two or more "letter + underscore" pairs, e.g. L_C_D_, T_V_.
ACRONYM = re.compile(r"\b((?:[A-Za-z]_){2,})")


def normalize_acronyms(text: str) -> str:
    """'the L_C_D_ screen' -> 'the lcd screen'.

    The transcripts write acronyms letter by letter. FTS5 would split L_C_D_ into
    the single letters L, C, D, so a search for "LCD" would find nothing.
    Used on stored text (search_text) AND on queries, so both sides match.
    """
    return ACRONYM.sub(lambda m: m.group(1).replace("_", "").lower(), text)


def is_filler(text: str) -> bool:
    """True for short back-channel turns like 'Mm-hmm.' or 'Okay, yeah.'"""
    words = re.findall(r"[a-z]+(?:-[a-z]+)*", text.lower())
    return len(words) <= config.FILLER_MAX_WORDS and all(w in config.FILLER_WORDS for w in words)


def parse_file(path: Path) -> tuple[dict, list[dict]]:
    """Read one transcript file -> (meeting metadata, list of turns)."""
    raw = path.read_text(encoding="utf-8")
    meta: dict = {}
    turns: list[dict] = []

    # Blocks are separated by blank lines: the header block, then one block per turn
    # (a "HH:MM:SS Speaker X" line followed by one line of text).
    for block in raw.split("\n\n"):
        lines = [ln.strip() for ln in block.strip().splitlines() if ln.strip()]
        if not lines:
            continue

        m = TURN_HEADER.match(lines[0])
        if m:
            hh, mm, ss, speaker = m.groups()
            text = " ".join(lines[1:])  # join defensively in case a turn ever spans lines
            if not text:
                continue
            turns.append({
                "turn_idx": len(turns),
                "speaker": speaker,
                "start_sec": int(hh) * 3600 + int(mm) * 60 + int(ss),
                "text": text,
            })
        else:
            # Header lines look like "Meeting: TS3009a", "Team: TS3009".
            for ln in lines:
                key, _, value = ln.partition(":")
                if key == "Meeting":
                    meta["meeting_id"] = value.strip()
                elif key == "Team":
                    meta["team"] = value.strip()

    if "meeting_id" not in meta or "team" not in meta:
        raise ValueError(f"{path.name}: missing Meeting/Team header")
    # The letter comes from the meeting id ('TS3009a' -> 'a'), NOT from counting files:
    # IS1002 has no meeting a, IS1005 has no meeting d.
    meta["letter"] = meta["meeting_id"][-1]
    return meta, turns


def ingest_meeting(conn: sqlite3.Connection, path: Path) -> int | None:
    """Store one meeting. Returns the number of turns, or None if already done."""
    meta, turns = parse_file(path)
    mid = meta["meeting_id"]

    row = conn.execute("SELECT parsed FROM meetings WHERE meeting_id = ?", (mid,)).fetchone()
    if row and row["parsed"]:
        return None  # resumability: finished on an earlier run, skip

    # One transaction per meeting: either everything for this meeting is saved,
    # or (on a crash) nothing is, and the next run redoes it cleanly.
    with conn:
        # Clear any half-written leftovers (only possible if an old run was killed
        # outside a transaction, but it makes re-running always safe).
        conn.execute("DELETE FROM turns_fts WHERE turn_id IN "
                     "(SELECT turn_id FROM turns WHERE meeting_id = ?)", (mid,))
        conn.execute("DELETE FROM turns WHERE meeting_id = ?", (mid,))
        conn.execute(
            "INSERT OR REPLACE INTO meetings (meeting_id, team, letter, n_turns, parsed) "
            "VALUES (?, ?, ?, ?, 0)",
            (mid, meta["team"], meta["letter"], len(turns)),
        )
        for t in turns:
            search_text = normalize_acronyms(t["text"])
            cur = conn.execute(
                "INSERT INTO turns (meeting_id, team, letter, turn_idx, speaker, start_sec,"
                " text, search_text, is_filler) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (mid, meta["team"], meta["letter"], t["turn_idx"], t["speaker"],
                 t["start_sec"], t["text"], search_text, int(is_filler(t["text"]))),
            )
            conn.execute("INSERT INTO turns_fts (search_text, turn_id) VALUES (?, ?)",
                         (search_text, cur.lastrowid))
        # Marked done LAST, inside the same transaction.
        conn.execute("UPDATE meetings SET parsed = 1 WHERE meeting_id = ?", (mid,))
    return len(turns)


def ingest_all(conn: sqlite3.Connection, transcripts_dir: Path = config.TRANSCRIPTS_DIR) -> None:
    db.init_db(conn)
    files = sorted(transcripts_dir.glob("*.txt"))
    if not files:
        raise FileNotFoundError(f"No transcripts found in {transcripts_dir}")
    done = skipped = 0
    for path in files:
        n = ingest_meeting(conn, path)
        if n is None:
            skipped += 1
        else:
            done += 1
            print(f"  {path.stem:<9} {n:>5} turns")
    total = conn.execute("SELECT COUNT(*) FROM turns").fetchone()[0]
    print(f"Ingested {done} meetings, skipped {skipped} already done. {total} turns in store.")


if __name__ == "__main__":
    ingest_all(db.connect())
