#!/usr/bin/env python3
"""Clean polluted Feishu session state caused by leaked global user profile."""

from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import sys
from datetime import datetime
from pathlib import Path
from typing import Iterable


SCRIPT_DIR = Path(__file__).resolve().parent
REPO_ROOT = SCRIPT_DIR.parent
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))


DEFAULT_PATTERNS = [
    "vik 大哥",
    "vik大哥",
    "你大哥",
    "大哥007",
    '称呼"大哥"',
    "称呼“大哥”",
    "User Identity: vik 大哥",
    "代号是 **你大哥**",
    "我是你大哥",
]

SESSION_ID_RE = re.compile(r"^(?:session_)?(?P<sid>\d{8}_\d{6}_[0-9a-f]{8})\.(?:json|jsonl)$")


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Clean contaminated Feishu session data from Hermes home.",
    )
    parser.add_argument(
        "--hermes-home",
        type=Path,
        default=Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes")),
        help="Hermes home directory. Defaults to active HERMES_HOME.",
    )
    parser.add_argument(
        "--source",
        default="feishu",
        help="Session source to clean. Default: feishu",
    )
    parser.add_argument(
        "--pattern",
        action="append",
        dest="patterns",
        help="Additional substring pattern to match. Can be repeated.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        help="Apply deletions. Without this flag the script is dry-run only.",
    )
    parser.add_argument(
        "--truncate-user-profile",
        action="store_true",
        help="Empty memories/USER.md after backing it up.",
    )
    parser.add_argument(
        "--backup-dir",
        type=Path,
        help="Backup directory for changed files. Default: <hermes_home>/cleanup-backups/<timestamp>/",
    )
    return parser.parse_args()


def build_backup_dir(hermes_home: Path, backup_dir: Path | None) -> Path:
    if backup_dir is not None:
        return backup_dir
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    return hermes_home / "cleanup-backups" / f"feishu_memory_cleanup_{stamp}"


def load_sessions_index(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def save_sessions_index(path: Path, data: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def extract_session_id(path: Path) -> str | None:
    match = SESSION_ID_RE.match(path.name)
    if not match:
        return None
    return match.group("sid")


def read_text_if_exists(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="ignore")
    except FileNotFoundError:
        return ""


def any_pattern_matches(text: str, patterns: Iterable[str]) -> bool:
    return any(pattern in text for pattern in patterns)


def find_source_session_ids(conn: sqlite3.Connection, source: str) -> set[str]:
    rows = conn.execute("select id from sessions where source = ?", (source,)).fetchall()
    return {row[0] for row in rows}


def find_db_matched_session_ids(
    conn: sqlite3.Connection,
    source: str,
    patterns: list[str],
) -> set[str]:
    clauses = []
    params: list[str] = [source]
    for pattern in patterns:
        like = f"%{pattern}%"
        clauses.append("coalesce(s.system_prompt, '') like ?")
        params.append(like)
        clauses.append("coalesce(m.content, '') like ?")
        params.append(like)
    if not clauses:
        return set()
    query = f"""
        select distinct s.id
        from sessions s
        left join messages m on m.session_id = s.id
        where s.source = ?
          and ({' or '.join(clauses)})
    """
    rows = conn.execute(query, params).fetchall()
    return {row[0] for row in rows}


def find_transcript_matches(
    sessions_dir: Path,
    candidate_session_ids: set[str],
    patterns: list[str],
) -> tuple[set[str], list[Path]]:
    matched_ids: set[str] = set()
    matched_paths: list[Path] = []
    if not sessions_dir.exists():
        return matched_ids, matched_paths

    for path in sessions_dir.iterdir():
        if not path.is_file():
            continue
        sid = extract_session_id(path)
        if not sid or sid not in candidate_session_ids:
            continue
        if any_pattern_matches(read_text_if_exists(path), patterns):
            matched_ids.add(sid)
            matched_paths.append(path)
    return matched_ids, matched_paths


def collect_session_files(sessions_dir: Path, session_ids: set[str]) -> list[Path]:
    paths: set[Path] = set()
    for sid in session_ids:
        for path in sessions_dir.glob(f"*{sid}*"):
            if path.is_file():
                paths.add(path)
    return sorted(paths)


def backup_files(files: list[Path], backup_dir: Path, root: Path) -> None:
    for path in files:
        if not path.exists():
            continue
        try:
            rel = path.relative_to(root)
        except ValueError:
            rel = Path(path.name)
        target = backup_dir / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy2(path, target)


def main() -> int:
    args = parse_args()
    hermes_home = args.hermes_home.expanduser().resolve()
    db_path = hermes_home / "state.db"
    sessions_dir = hermes_home / "sessions"
    sessions_index_path = sessions_dir / "sessions.json"
    user_profile_path = hermes_home / "memories" / "USER.md"
    patterns = list(DEFAULT_PATTERNS)
    if args.patterns:
        patterns.extend(args.patterns)

    if not db_path.exists():
        print(f"state.db not found: {db_path}", file=sys.stderr)
        return 2

    sessions_index = load_sessions_index(sessions_index_path)
    source_index_session_ids = {
        str(entry.get("session_id"))
        for entry in sessions_index.values()
        if isinstance(entry, dict) and entry.get("platform") == args.source and entry.get("session_id")
    }

    try:
        conn = sqlite3.connect(db_path)
        conn.row_factory = sqlite3.Row
        source_db_session_ids = find_source_session_ids(conn, args.source)
        matched_db_session_ids = find_db_matched_session_ids(conn, args.source, patterns)
    except sqlite3.OperationalError as exc:
        message = str(exc)
        if "fts5" in message.lower():
            print(
                "SQLite on this machine lacks FTS5 support. "
                "Run this script inside the hermes-agent image/container instead.",
                file=sys.stderr,
            )
        else:
            print(f"SQLite error: {message}", file=sys.stderr)
        return 3
    candidate_session_ids = source_db_session_ids | source_index_session_ids
    matched_transcript_session_ids, direct_matched_paths = find_transcript_matches(
        sessions_dir,
        candidate_session_ids,
        patterns,
    )
    matched_session_ids = matched_db_session_ids | matched_transcript_session_ids
    transcript_paths = collect_session_files(sessions_dir, matched_session_ids)
    if not transcript_paths:
        transcript_paths = direct_matched_paths

    sessions_index_keys_to_remove = sorted(
        key
        for key, entry in sessions_index.items()
        if isinstance(entry, dict) and entry.get("session_id") in matched_session_ids
    )

    print(f"HERMES_HOME: {hermes_home}")
    print(f"source: {args.source}")
    print(f"patterns: {patterns}")
    print(f"source sessions in db: {len(source_db_session_ids)}")
    print(f"source sessions in sessions.json: {len(source_index_session_ids)}")
    print(f"matched session ids: {len(matched_session_ids)}")
    for sid in sorted(matched_session_ids):
        print(f"  session: {sid}")
    print(f"matched transcript files: {len(transcript_paths)}")
    for path in transcript_paths:
        print(f"  file: {path}")
    print(f"sessions.json entries to remove: {len(sessions_index_keys_to_remove)}")
    for key in sessions_index_keys_to_remove:
        print(f"  index: {key}")
    if args.truncate_user_profile:
        print(f"USER.md will be truncated: {user_profile_path}")

    if not args.apply:
        print("Dry run only. Re-run with --apply after stopping the gateway.")
        conn.close()
        return 0

    backup_dir = build_backup_dir(hermes_home, args.backup_dir)
    backup_files(
        [
            db_path,
            db_path.with_name("state.db-wal"),
            db_path.with_name("state.db-shm"),
            sessions_index_path,
            *transcript_paths,
            user_profile_path if args.truncate_user_profile else Path("/nonexistent"),
        ],
        backup_dir,
        hermes_home,
    )
    print(f"Backups written to: {backup_dir}")

    for sid in sorted(matched_session_ids):
        conn.execute("delete from messages where session_id = ?", (sid,))
        conn.execute("delete from sessions where id = ?", (sid,))
    conn.commit()
    conn.close()

    for path in transcript_paths:
        try:
            path.unlink()
        except FileNotFoundError:
            pass

    if sessions_index_keys_to_remove:
        for key in sessions_index_keys_to_remove:
            sessions_index.pop(key, None)
        save_sessions_index(sessions_index_path, sessions_index)

    if args.truncate_user_profile:
        user_profile_path.parent.mkdir(parents=True, exist_ok=True)
        user_profile_path.write_text("", encoding="utf-8")

    print("Cleanup complete. Restart the gateway before sending new Feishu messages.")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
