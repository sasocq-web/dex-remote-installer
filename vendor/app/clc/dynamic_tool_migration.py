"""Migrate the authoritative rollout catalog before app-server opens a writer.

SQLite thread_dynamic_tools is only an index. Resume restores session_meta.
Native writer locks protect live threads. A private recovery journal and a
transactional offset ledger preserve paginated history across crashes.
"""
from __future__ import annotations
import contextlib, fcntl, hashlib, json, os, re, shutil, sqlite3, stat, uuid
from pathlib import Path

MAX_META = 16 * 1024 * 1024

def specs():
    from .automations import AUTOMATION_TOOL_SPEC
    from .android_lifecycle import ANDROID_TOOL_SPEC
    from .lab_lifecycle import LAB_TOOL_SPEC
    from .build_lifecycle import BUILD_TOOL_SPEC
    from .publication_lifecycle import PUBLICATION_TOOL_SPEC
    from .project_lifecycle import PROJECT_TOOL_SPEC
    return [PROJECT_TOOL_SPEC, AUTOMATION_TOOL_SPEC, ANDROID_TOOL_SPEC, LAB_TOOL_SPEC, BUILD_TOOL_SPEC, PUBLICATION_TOOL_SPEC]

def merge_tools(old):
    if old is None:
        old = []
    if not isinstance(old, list) or any(not isinstance(x, dict) for x in old):
        raise ValueError("Invalid persisted dynamic tools")
    wanted = {s["name"]: s for s in specs()}
    result, seen = [], set()
    for item in old:
        name = item.get("name")
        if name in wanted and not item.get("namespace"):
            if name not in seen:
                result.append(wanted[name])
                seen.add(name)
        else:
            result.append(item)
    result.extend(v for k, v in wanted.items() if k not in seen)
    return result

def digest(path):
    with path.open("rb") as f:
        return hashlib.file_digest(f, "sha256").hexdigest()

def sync_dir(path):
    fd = os.open(path, os.O_DIRECTORY)
    try: os.fsync(fd)
    finally: os.close(fd)

def save_json(path, value):
    tmp = path.with_suffix(".tmp")
    with tmp.open("w") as f:
        os.chmod(tmp, 0o600)
        json.dump(value, f, ensure_ascii=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    sync_dir(path.parent)

@contextlib.contextmanager
def writer_lock(home, thread):
    locks = home / "thread-writer-locks"
    locks.mkdir(mode=0o700, exist_ok=True)
    with (locks / ".coordination.lock").open("a+b") as coord:
        fcntl.flock(coord, fcntl.LOCK_EX)
        lock = (locks / (thread + ".lock")).open("a+b")
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            lock.close()
            lock = None
        fcntl.flock(coord, fcntl.LOCK_UN)
        try:
            yield lock is not None
        finally:
            if lock:
                # Match the native coordinator: never unlink a lock someone
                # else may have acquired; keeping the inode is safe.
                fcntl.flock(coord, fcntl.LOCK_EX)
                lock.close()
                fcntl.flock(coord, fcntl.LOCK_UN)

def finish(home, journal, fault=None):
    data = json.loads(journal.read_text())
    path, backup = Path(data["path"]), Path(data["backup"])
    dbfile = home / data["database"]
    if not path.resolve().is_relative_to(home) or not backup.resolve().is_relative_to(home):
        raise ValueError("Migration journal outside Codex home")
    with sqlite3.connect(dbfile, timeout=30) as db:
        db.execute("BEGIN IMMEDIATE")
        db.execute("CREATE TABLE IF NOT EXISTS sasocq_tool_migrations (id TEXT PRIMARY KEY, thread_id TEXT NOT NULL, delta INTEGER NOT NULL)")
        done = db.execute("SELECT 1 FROM sasocq_tool_migrations WHERE id=?", (data["id"],)).fetchone()
        if done:
            journal.rename(journal.with_suffix(".complete"))
            sync_dir(journal.parent)
            return
        current = digest(path)
        if current == data["old_hash"]:
            # Journal was durable before replacement. No index was modified.
            journal.rename(journal.with_suffix(".aborted"))
            sync_dir(journal.parent)
            return
        if current != data["new_hash"]:
            raise RuntimeError("Rollout changed during interrupted migration; preserve and stop")
        delta, boundary, thread = data["delta"], data["old_length"], data["thread"]
        tables = {r[0] for r in db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        if "thread_turns" in tables:
            columns = {r[1] for r in db.execute("PRAGMA table_info(thread_turns)")}
            for col in ("rollout_byte_offset", "rollout_end_byte_offset"):
                if col in columns:
                    db.execute(f"UPDATE thread_turns SET {col}={col}+? WHERE thread_id=? AND {col}>=?", (delta, thread, boundary))
        if "thread_history_projection_state" in tables:
            db.execute("UPDATE thread_history_projection_state SET next_rollout_byte_offset=next_rollout_byte_offset+? WHERE thread_id=? AND next_rollout_byte_offset>=?", (delta, thread, boundary))
        db.execute("INSERT INTO sasocq_tool_migrations VALUES (?,?,?)", (data["id"], thread, delta))
        if fault: fault("before_commit")
        db.commit()
    if fault: fault("after_commit")
    journal.rename(journal.with_suffix(".complete"))
    sync_dir(journal.parent)

def migrate_one(home, thread, path, fault=None):
    if not re.fullmatch(r"[0-9a-fA-F-]{36}", thread):
        return "invalid"
    if not path.is_file() or path.is_symlink() or not path.resolve().is_relative_to(home):
        return "missing"
    with writer_lock(home, thread) as acquired:
        if not acquired:
            return "busy"
        area = home / "sasocq-tool-migrations" / thread
        if area.exists():
            for journal in sorted(area.glob("*.pending")):
                finish(home, journal)
        with path.open("rb") as f:
            old = f.readline(MAX_META + 1)
        if len(old) > MAX_META or not old.endswith(b"\n"):
            raise ValueError("Invalid rollout metadata length")
        record = json.loads(old)
        payload = record.get("payload", {})
        if record.get("type") != "session_meta" or payload.get("id") != thread:
            raise ValueError("Rollout session metadata does not match thread")
        wanted = merge_tools(payload.get("dynamic_tools"))
        if payload.get("dynamic_tools") == wanted:
            return "current"
        payload["dynamic_tools"] = wanted
        encoded = json.dumps(record, ensure_ascii=False, separators=(",", ":")).encode()
        # Reserve room for future schema edits while preserving all byte offsets.
        size = len(old) if len(encoded) < len(old) else len(encoded) + 4097
        new = encoded + b" " * (size - len(encoded) - 1) + b"\n"
        area.mkdir(parents=True, mode=0o700, exist_ok=True)
        os.chmod(area.parent, 0o700)
        migration = uuid.uuid4().hex
        backup = area / (migration + ".jsonl")
        shutil.copy2(path, backup)
        os.chmod(backup, 0o600)
        with backup.open("rb") as f: os.fsync(f.fileno())
        temp = path.with_name("." + path.name + "." + migration)
        info = path.stat()
        try:
            with path.open("rb") as source, temp.open("xb") as target:
                source.seek(len(old))
                target.write(new)
                shutil.copyfileobj(source, target, 1024 * 1024)
                target.flush()
                os.fsync(target.fileno())
            shutil.copystat(path, temp)
            os.chmod(temp, stat.S_IMODE(info.st_mode))
            os.utime(temp, ns=(info.st_atime_ns, info.st_mtime_ns))
            journal = area / (migration + ".pending")
            data = dict(id=migration, thread=thread, path=str(path), backup=str(backup),
                        old_hash=digest(backup), new_hash=digest(temp),
                        old_length=len(old), delta=len(new)-len(old),
                        database="thread_history_1.sqlite" if (home/"thread_history_1.sqlite").is_file() else "state_5.sqlite")
            save_json(journal, data)
            if fault: fault("before_replace")
            os.replace(temp, path)
            sync_dir(path.parent)
            if fault: fault("after_replace")
            finish(home, journal, fault)
        finally:
            temp.unlink(missing_ok=True)
        return "migrated"

def migrate_rollout_tools(database):
    database = Path(database)
    if not database.is_file():
        return {}
    home = database.parent.resolve()
    with sqlite3.connect("file:" + str(database) + "?mode=ro", uri=True) as db:
        columns = {r[1] for r in db.execute("PRAGMA table_info(threads)")}
        if not {"id", "rollout_path"} <= columns:
            return {}
        rows = db.execute("SELECT id,rollout_path FROM threads").fetchall()
    counts = {}
    for thread, filename in rows:
        result = migrate_one(home, thread, Path(filename)) if filename else "missing"
        counts[result] = counts.get(result, 0) + 1
    report = home / "sasocq-tool-migrations" / "latest.json"
    report.parent.mkdir(mode=0o700, exist_ok=True)
    save_json(report, {"counts": counts, "catalog_sha256": hashlib.sha256(json.dumps(specs(), sort_keys=True).encode()).hexdigest()})
    return counts
