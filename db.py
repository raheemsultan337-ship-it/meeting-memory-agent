"""SQLite schema and connection helper for the memory store.

Layers (more tables arrive in later steps):
  episodic  -> meetings, turns, turns_fts   (this step)
  semantic  -> facts                        (step 5)
  temporal  -> timeline                     (step 6)
"""
import sqlite3
from pathlib import Path

import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS meetings (
    meeting_id  TEXT PRIMARY KEY,          -- 'TS3009a'
    team        TEXT NOT NULL,             -- 'TS3009'
    letter      TEXT NOT NULL,             -- 'a'..'d' (read from the header, never assumed)
    n_turns     INTEGER,
    parsed      INTEGER NOT NULL DEFAULT 0, -- 1 once the meeting is fully stored
    chunked     INTEGER NOT NULL DEFAULT 0, -- 1 once its chunks are built (step 3)
    extracted   INTEGER NOT NULL DEFAULT 0  -- 1 once its facts are extracted (step 5)
);

CREATE TABLE IF NOT EXISTS turns (
    turn_id     INTEGER PRIMARY KEY,       -- global id, used to join with turns_fts
    meeting_id  TEXT NOT NULL REFERENCES meetings(meeting_id),
    team        TEXT NOT NULL,             -- denormalised so filters need no join
    letter      TEXT NOT NULL,
    turn_idx    INTEGER NOT NULL,          -- 0,1,2... within the meeting; citations point here
    speaker     TEXT NOT NULL,             -- 'A'..'D'
    start_sec   INTEGER NOT NULL,          -- '00:01:05' -> 65
    text        TEXT NOT NULL,             -- ORIGINAL words, never modified (citations use this)
    search_text TEXT NOT NULL,             -- normalised copy, used only for searching
    is_filler   INTEGER NOT NULL,
    UNIQUE (meeting_id, turn_idx)
);
CREATE INDEX IF NOT EXISTS idx_turns_team ON turns(team, letter);

-- Full-text index (BM25). porter = stemming (decide/decided/decision),
-- unicode61 = case-insensitive tokenising.
CREATE VIRTUAL TABLE IF NOT EXISTS turns_fts USING fts5(
    search_text,
    turn_id UNINDEXED,
    tokenize = 'porter unicode61'
);

-- Chunks: windows of consecutive non-filler turns (~250 words), the unit for
-- meaning-based search. One turn can sit in two chunks (overlap).
CREATE TABLE IF NOT EXISTS chunks (
    chunk_id    INTEGER PRIMARY KEY,
    meeting_id  TEXT NOT NULL REFERENCES meetings(meeting_id),
    team        TEXT NOT NULL,
    letter      TEXT NOT NULL,
    start_idx   INTEGER NOT NULL,      -- first turn_idx covered
    end_idx     INTEGER NOT NULL,      -- last turn_idx covered
    n_words     INTEGER NOT NULL,
    text        TEXT NOT NULL          -- one "Speaker: words" line per turn (acronyms normalised); what gets embedded
);
CREATE INDEX IF NOT EXISTS idx_chunks_team ON chunks(team, meeting_id);

-- Keyword index over chunks too, so BM25 and vectors rank the SAME unit and
-- their two rankings can be fused.
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
    text,
    chunk_id UNINDEXED,
    tokenize = 'porter unicode61'
);

-- One embedding per chunk: 768 float32 numbers, L2-normalised, stored as bytes.
CREATE TABLE IF NOT EXISTS chunk_vectors (
    chunk_id    INTEGER PRIMARY KEY REFERENCES chunks(chunk_id),
    vec         BLOB NOT NULL
);

-- SEMANTIC memory: facts extracted per meeting (LLM), each with a verified quote.
CREATE TABLE IF NOT EXISTS facts (
    fact_id     INTEGER PRIMARY KEY,
    meeting_id  TEXT NOT NULL REFERENCES meetings(meeting_id),
    team        TEXT NOT NULL,
    letter      TEXT NOT NULL,
    type        TEXT NOT NULL,   -- decision|proposal|change|action|followup|number|disagreement
    topic       TEXT NOT NULL,
    statement   TEXT NOT NULL,
    speaker     TEXT NOT NULL,   -- from the turns table, not from the model
    owner       TEXT,            -- for actions
    turn_idx    INTEGER NOT NULL,
    quote       TEXT NOT NULL,   -- passed the citation gate
    value       REAL, unit TEXT  -- for numbers: "twelve Euros fifty" -> 12.5 euro
);
CREATE INDEX IF NOT EXISTS idx_facts_team ON facts(team, letter);

CREATE TABLE IF NOT EXISTS speaker_roles (
    team TEXT NOT NULL, speaker TEXT NOT NULL, role TEXT NOT NULL,
    meeting_id TEXT, turn_idx INTEGER,
    PRIMARY KEY (team, speaker)
);

-- TEMPORAL memory: per team and topic, the history of a decision and what is current.
CREATE TABLE IF NOT EXISTS timeline (
    team        TEXT NOT NULL,
    topic       TEXT NOT NULL,
    current     TEXT NOT NULL,   -- the state as of the team's latest meeting
    history     TEXT NOT NULL,   -- JSON list: [{"meeting_id", "state", "fact_id"}], oldest first
    status      TEXT NOT NULL,   -- settled | changed | open | dropped
    open_items  TEXT,            -- unresolved actions/disagreements on this topic
    PRIMARY KEY (team, topic)
);
"""

# Columns added after a table first shipped. CREATE TABLE IF NOT EXISTS never
# changes an existing table, so init_db adds these to older databases.
MIGRATIONS = [
    ("meetings", "chunked", "INTEGER NOT NULL DEFAULT 0"),
    ("meetings", "extracted", "INTEGER NOT NULL DEFAULT 0"),
]


def connect(path: Path | str = config.DB_PATH) -> sqlite3.Connection:
    """Open the store. Rows come back as sqlite3.Row so you can use row['text']."""
    conn = sqlite3.connect(path, timeout=30)  # wait for locks (background jobs write too)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys = ON")
    return conn


def init_db(conn: sqlite3.Connection) -> None:
    """Create all tables if they don't exist yet, and add any missing columns.
    Safe to call every run."""
    conn.executescript(SCHEMA)
    for table, column, decl in MIGRATIONS:
        existing = {r["name"] for r in conn.execute(f"PRAGMA table_info({table})")}
        if column not in existing:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {column} {decl}")
    conn.commit()
