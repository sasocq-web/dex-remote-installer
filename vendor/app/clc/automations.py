from __future__ import annotations

import asyncio
import json
import logging
import os
import sqlite3
import time
import uuid
from contextlib import closing
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Awaitable, Callable, Dict
from zoneinfo import ZoneInfo


LOGGER = logging.getLogger(__name__)
LOCAL_TIMEZONE = ZoneInfo("America/Sao_Paulo")
ACTIVE = "ACTIVE"
PAUSED = "PAUSED"


AUTOMATION_TOOL_DESCRIPTION = (
    "Cria, atualiza, consulta ou exclui agendamentos recorrentes no Dex SASOCQ. "
    "Use esta ferramenta quando o usuário pedir uma tarefa agendada, automação, "
    "lembrete recorrente ou monitoramento periódico. Agendamentos heartbeat continuam "
    "a conversa atual; agendamentos cron iniciam uma execução independente no projeto. "
    "Use RRULE sem DTSTART, por exemplo FREQ=DAILY;BYHOUR=9;BYMINUTE=0."
)

AUTOMATION_INPUT_SCHEMA: Dict[str, Any] = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "mode": {
            "type": "string",
            "enum": ["view", "create", "suggested_create", "update", "suggested_update", "delete"],
        },
        "automationId": {"type": "string"},
        "name": {"type": "string", "minLength": 1, "maxLength": 120},
        "prompt": {"type": "string", "minLength": 1, "maxLength": 20000},
        "status": {"type": "string", "enum": [ACTIVE, PAUSED]},
        "rrule": {"type": "string", "minLength": 1, "maxLength": 500},
        "kind": {"type": "string", "enum": ["heartbeat", "cron"]},
        "targetThreadId": {"type": "string"},
        "projectId": {"type": "string"},
        "model": {"type": "string"},
        "reasoningEffort": {"type": "string"},
        "executionEnvironment": {"type": "string", "enum": ["local"]},
    },
    "required": ["mode"],
}

AUTOMATION_TOOL_SPEC: Dict[str, Any] = {
    "type": "function",
    "name": "automation_update",
    "description": AUTOMATION_TOOL_DESCRIPTION,
    "inputSchema": AUTOMATION_INPUT_SCHEMA,
    "deferLoading": False,
}


class AutomationValidationError(ValueError):
    pass


def _parse_rrule(value: str) -> dict[str, str]:
    text = str(value or "").strip().upper()
    if text.startswith("RRULE:"):
        text = text[6:]
    parts: dict[str, str] = {}
    for chunk in text.split(";"):
        key, separator, raw = chunk.partition("=")
        key = key.strip()
        raw = raw.strip()
        if not separator or not key or not raw or key in parts:
            raise AutomationValidationError("RRULE inválida")
        parts[key] = raw
    allowed = {"FREQ", "INTERVAL", "BYMINUTE", "BYHOUR", "BYDAY", "BYMONTHDAY"}
    unsupported = sorted(set(parts) - allowed)
    if unsupported:
        raise AutomationValidationError(f"RRULE contém campos não suportados: {', '.join(unsupported)}")
    frequency = parts.get("FREQ", "")
    if frequency not in {"MINUTELY", "HOURLY", "DAILY", "WEEKLY", "MONTHLY"}:
        raise AutomationValidationError("FREQ deve ser MINUTELY, HOURLY, DAILY, WEEKLY ou MONTHLY")
    try:
        interval = int(parts.get("INTERVAL", "1"))
    except ValueError as exc:
        raise AutomationValidationError("INTERVAL deve ser inteiro") from exc
    if interval < 1 or interval > 10_000:
        raise AutomationValidationError("INTERVAL deve ficar entre 1 e 10000")
    parts["INTERVAL"] = str(interval)
    for key, lower, upper in (("BYMINUTE", 0, 59), ("BYHOUR", 0, 23), ("BYMONTHDAY", 1, 31)):
        if key not in parts:
            continue
        try:
            values = [int(item) for item in parts[key].split(",")]
        except ValueError as exc:
            raise AutomationValidationError(f"{key} deve conter números inteiros") from exc
        if not values or any(item < lower or item > upper for item in values):
            raise AutomationValidationError(f"{key} está fora do intervalo permitido")
        parts[key] = ",".join(str(item) for item in sorted(set(values)))
    if "BYDAY" in parts:
        days = [item.strip() for item in parts["BYDAY"].split(",")]
        allowed_days = {"MO", "TU", "WE", "TH", "FR", "SA", "SU"}
        if not days or any(item not in allowed_days for item in days):
            raise AutomationValidationError("BYDAY deve usar MO,TU,WE,TH,FR,SA,SU")
        parts["BYDAY"] = ",".join(dict.fromkeys(days))
    order = ("FREQ", "INTERVAL", "BYDAY", "BYMONTHDAY", "BYHOUR", "BYMINUTE")
    return {key: parts[key] for key in order if key in parts}


def normalize_rrule(value: str) -> str:
    return ";".join(f"{key}={raw}" for key, raw in _parse_rrule(value).items())


def next_occurrence(rrule: str, after: float, anchor: float) -> float:
    rule = _parse_rrule(rrule)
    frequency = rule["FREQ"]
    interval = int(rule["INTERVAL"])
    after_local = datetime.fromtimestamp(after, LOCAL_TIMEZONE)
    anchor_local = datetime.fromtimestamp(anchor, LOCAL_TIMEZONE).replace(second=0, microsecond=0)
    candidate = after_local.replace(second=0, microsecond=0) + timedelta(minutes=1)
    default_minutes = ",".join(str(item) for item in range(60)) if frequency == "MINUTELY" else str(anchor_local.minute)
    minutes = {int(item) for item in rule.get("BYMINUTE", default_minutes).split(",")}
    hours = {int(item) for item in rule.get("BYHOUR", str(anchor_local.hour)).split(",")}
    weekdays = {"MO": 0, "TU": 1, "WE": 2, "TH": 3, "FR": 4, "SA": 5, "SU": 6}
    days = {weekdays[item] for item in rule.get("BYDAY", list(weekdays)[anchor_local.weekday()]).split(",")}
    month_days = {int(item) for item in rule.get("BYMONTHDAY", str(anchor_local.day)).split(",")}
    maximum = candidate + timedelta(days=366 * 6)
    anchor_week = anchor_local.date() - timedelta(days=anchor_local.weekday())

    while candidate <= maximum:
        elapsed_minutes = int((candidate - anchor_local).total_seconds() // 60)
        elapsed_hours = int((candidate.replace(minute=0) - anchor_local.replace(minute=0)).total_seconds() // 3600)
        elapsed_days = (candidate.date() - anchor_local.date()).days
        elapsed_weeks = ((candidate.date() - timedelta(days=candidate.weekday())) - anchor_week).days // 7
        elapsed_months = (candidate.year - anchor_local.year) * 12 + candidate.month - anchor_local.month
        matches = candidate.minute in minutes
        if frequency == "MINUTELY":
            matches = matches and elapsed_minutes >= 0 and elapsed_minutes % interval == 0
        elif frequency == "HOURLY":
            matches = matches and elapsed_hours >= 0 and elapsed_hours % interval == 0
        elif frequency == "DAILY":
            matches = matches and candidate.hour in hours and elapsed_days >= 0 and elapsed_days % interval == 0
        elif frequency == "WEEKLY":
            matches = (
                matches
                and candidate.hour in hours
                and candidate.weekday() in days
                and elapsed_weeks >= 0
                and elapsed_weeks % interval == 0
            )
        else:
            matches = (
                matches
                and candidate.hour in hours
                and candidate.day in month_days
                and elapsed_months >= 0
                and elapsed_months % interval == 0
            )
        if matches:
            return candidate.timestamp()
        candidate += timedelta(minutes=1)
    raise AutomationValidationError("não foi possível calcular a próxima execução da RRULE")


class AutomationStore:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._initialize()

    def _connect(self) -> sqlite3.Connection:
        connection = sqlite3.connect(self.path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA journal_mode=WAL")
        connection.execute("PRAGMA foreign_keys=ON")
        return connection

    def _initialize(self) -> None:
        with closing(self._connect()) as connection, connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS automations (
                    id TEXT PRIMARY KEY,
                    workspace TEXT NOT NULL,
                    project_id TEXT NOT NULL,
                    target_thread_id TEXT NOT NULL DEFAULT '',
                    kind TEXT NOT NULL,
                    name TEXT NOT NULL,
                    prompt TEXT NOT NULL,
                    status TEXT NOT NULL,
                    rrule TEXT NOT NULL,
                    model TEXT,
                    reasoning_effort TEXT,
                    execution_environment TEXT NOT NULL DEFAULT 'local',
                    next_run_at REAL,
                    last_run_at REAL,
                    created_at REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE INDEX IF NOT EXISTS automations_due_idx
                    ON automations(status, next_run_at);
                CREATE TABLE IF NOT EXISTS automation_runs (
                    id TEXT PRIMARY KEY,
                    automation_id TEXT NOT NULL REFERENCES automations(id) ON DELETE CASCADE,
                    thread_id TEXT NOT NULL DEFAULT '',
                    status TEXT NOT NULL,
                    error TEXT NOT NULL DEFAULT '',
                    started_at REAL NOT NULL,
                    completed_at REAL
                );
                CREATE INDEX IF NOT EXISTS automation_runs_automation_idx
                    ON automation_runs(automation_id, started_at DESC);
                """
            )
        os.chmod(self.path, 0o600)

    @staticmethod
    def _record(row: sqlite3.Row) -> dict[str, Any]:
        value = dict(row)
        for key in ("next_run_at", "last_run_at", "created_at", "updated_at"):
            if value.get(key) is not None:
                value[key] = float(value[key])
        return value

    def list(self, workspace: str, automation_id: str = "") -> list[dict[str, Any]]:
        with closing(self._connect()) as connection, connection:
            if automation_id:
                rows = connection.execute(
                    "SELECT * FROM automations WHERE workspace=? AND id=?", (workspace, automation_id)
                ).fetchall()
            else:
                rows = connection.execute(
                    "SELECT * FROM automations WHERE workspace=? ORDER BY created_at DESC", (workspace,)
                ).fetchall()
        return [self._record(row) for row in rows]

    def list_all(self) -> list[dict[str, Any]]:
        with closing(self._connect()) as connection, connection:
            rows = connection.execute(
                """SELECT * FROM automations
                   ORDER BY CASE status WHEN 'ACTIVE' THEN 0 ELSE 1 END,
                            CASE WHEN next_run_at IS NULL THEN 1 ELSE 0 END,
                            next_run_at, created_at DESC"""
            ).fetchall()
        return [self._record(row) for row in rows]

    def create(self, workspace: str, thread_id: str, values: dict[str, Any]) -> dict[str, Any]:
        now = time.time()
        kind = str(values.get("kind") or "heartbeat")
        if kind not in {"heartbeat", "cron"}:
            raise AutomationValidationError("kind deve ser heartbeat ou cron")
        if str(values.get("executionEnvironment") or "local") != "local":
            raise AutomationValidationError("somente a execução local está disponível")
        workspace_project = workspace.split(":", 1)[1] if workspace.startswith("project:") else ""
        requested_project = str(values.get("projectId") or "")
        if workspace_project and requested_project and requested_project != workspace_project:
            raise AutomationValidationError("o agendamento não pode sair do projeto atual")
        project_id = workspace_project or requested_project or "system-control"
        target_thread_id = str(values.get("targetThreadId") or (thread_id if kind == "heartbeat" else ""))
        if kind == "heartbeat" and not target_thread_id:
            raise AutomationValidationError("targetThreadId é obrigatório para heartbeat")
        prompt = str(values.get("prompt") or "").strip()
        if not prompt:
            raise AutomationValidationError("prompt é obrigatório")
        name = str(values.get("name") or prompt.splitlines()[0][:80]).strip()
        if not name:
            raise AutomationValidationError("name é obrigatório")
        status = str(values.get("status") or ACTIVE).upper()
        if status not in {ACTIVE, PAUSED}:
            raise AutomationValidationError("status deve ser ACTIVE ou PAUSED")
        rrule = normalize_rrule(str(values.get("rrule") or "FREQ=DAILY;BYHOUR=9;BYMINUTE=0"))
        next_run = next_occurrence(rrule, now, now) if status == ACTIVE else None
        automation_id = uuid.uuid4().hex
        record = {
            "id": automation_id,
            "workspace": workspace,
            "project_id": project_id,
            "target_thread_id": target_thread_id,
            "kind": kind,
            "name": name[:120],
            "prompt": prompt,
            "status": status,
            "rrule": rrule,
            "model": str(values.get("model") or "") or None,
            "reasoning_effort": str(values.get("reasoningEffort") or "") or None,
            "execution_environment": "local",
            "next_run_at": next_run,
            "last_run_at": None,
            "created_at": now,
            "updated_at": now,
        }
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """INSERT INTO automations
                   (id, workspace, project_id, target_thread_id, kind, name, prompt, status, rrule,
                    model, reasoning_effort, execution_environment, next_run_at, last_run_at, created_at, updated_at)
                   VALUES (:id,:workspace,:project_id,:target_thread_id,:kind,:name,:prompt,:status,:rrule,
                    :model,:reasoning_effort,:execution_environment,:next_run_at,:last_run_at,:created_at,:updated_at)""",
                record,
            )
        return record

    def update(self, workspace: str, automation_id: str, values: dict[str, Any]) -> dict[str, Any]:
        existing = self.list(workspace, automation_id)
        if not existing:
            raise AutomationValidationError("agendamento não encontrado")
        record = existing[0]
        mapping = {
            "name": "name",
            "prompt": "prompt",
            "status": "status",
            "kind": "kind",
            "targetThreadId": "target_thread_id",
            "projectId": "project_id",
            "model": "model",
            "reasoningEffort": "reasoning_effort",
        }
        for source, target in mapping.items():
            if source in values:
                if source == "projectId" and workspace.startswith("project:"):
                    expected = workspace.split(":", 1)[1]
                    if str(values[source] or "") != expected:
                        raise AutomationValidationError("o agendamento não pode sair do projeto atual")
                record[target] = str(values[source] or "").strip() or None
        if "status" in values:
            record["status"] = str(values["status"]).upper()
        if "rrule" in values:
            record["rrule"] = normalize_rrule(str(values["rrule"]))
        if record["kind"] == "heartbeat" and not record.get("target_thread_id"):
            raise AutomationValidationError("targetThreadId é obrigatório para heartbeat")
        if record["kind"] not in {"heartbeat", "cron"}:
            raise AutomationValidationError("kind deve ser heartbeat ou cron")
        if record["status"] not in {ACTIVE, PAUSED}:
            raise AutomationValidationError("status deve ser ACTIVE ou PAUSED")
        if not record.get("name") or not record.get("prompt"):
            raise AutomationValidationError("name e prompt não podem ficar vazios")
        now = time.time()
        record["updated_at"] = now
        record["next_run_at"] = next_occurrence(record["rrule"], now, record["created_at"]) if record["status"] == ACTIVE else None
        with closing(self._connect()) as connection, connection:
            connection.execute(
                """UPDATE automations SET project_id=:project_id,target_thread_id=:target_thread_id,kind=:kind,
                   name=:name,prompt=:prompt,status=:status,rrule=:rrule,model=:model,
                   reasoning_effort=:reasoning_effort,next_run_at=:next_run_at,updated_at=:updated_at
                   WHERE id=:id AND workspace=:workspace""",
                record,
            )
        return record

    def delete(self, workspace: str, automation_id: str) -> bool:
        with closing(self._connect()) as connection, connection:
            cursor = connection.execute(
                "DELETE FROM automations WHERE workspace=? AND id=?", (workspace, automation_id)
            )
        return cursor.rowcount > 0

    def claim_due(self, now: float | None = None, limit: int = 10) -> list[dict[str, Any]]:
        now = float(now if now is not None else time.time())
        claimed: list[dict[str, Any]] = []
        with closing(self._connect()) as connection, connection:
            connection.execute("BEGIN IMMEDIATE")
            rows = connection.execute(
                """SELECT * FROM automations
                   WHERE status=? AND next_run_at IS NOT NULL AND next_run_at<=?
                   ORDER BY next_run_at LIMIT ?""",
                (ACTIVE, now, int(limit)),
            ).fetchall()
            for row in rows:
                record = self._record(row)
                next_run = next_occurrence(record["rrule"], now, record["created_at"])
                connection.execute(
                    "UPDATE automations SET last_run_at=?,next_run_at=?,updated_at=? WHERE id=?",
                    (now, next_run, now, record["id"]),
                )
                record["last_run_at"] = now
                record["next_run_at"] = next_run
                claimed.append(record)
        return claimed

    def start_run(self, automation_id: str) -> str:
        run_id = uuid.uuid4().hex
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "INSERT INTO automation_runs(id,automation_id,status,started_at) VALUES (?,?,?,?)",
                (run_id, automation_id, "STARTING", time.time()),
            )
        return run_id

    def finish_run(self, run_id: str, thread_id: str = "", error: str = "") -> None:
        with closing(self._connect()) as connection, connection:
            connection.execute(
                "UPDATE automation_runs SET thread_id=?,status=?,error=?,completed_at=? WHERE id=?",
                (thread_id, "FAILED" if error else "STARTED", error[:4000], time.time(), run_id),
            )


class AutomationManager:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        self._store: AutomationStore | None = None

    @property
    def store(self) -> AutomationStore:
        if self._store is None:
            self._store = AutomationStore(self.path)
        return self._store

    async def handle_server_request(self, workspace: str, message: dict[str, Any]) -> dict[str, Any] | None:
        if message.get("method") != "item/tool/call":
            return None
        params = message.get("params") or {}
        if params.get("tool") != AUTOMATION_TOOL_SPEC["name"]:
            return None
        arguments = params.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        thread_id = str(params.get("threadId") or "")
        try:
            result = await asyncio.to_thread(self._apply, workspace, thread_id, arguments)
            return {
                "success": True,
                "contentItems": [{"type": "inputText", "text": json.dumps(result, ensure_ascii=False)}],
            }
        except Exception as exc:
            LOGGER.warning("Falha no agendamento solicitado por %s: %s", workspace, exc)
            return {
                "success": False,
                "contentItems": [{"type": "inputText", "text": str(exc)}],
            }

    def _apply(self, workspace: str, thread_id: str, arguments: dict[str, Any]) -> dict[str, Any]:
        mode = str(arguments.get("mode") or "").casefold()
        automation_id = str(arguments.get("automationId") or "")
        if mode == "view":
            return {"automations": self.store.list(workspace, automation_id)}
        if mode in {"create", "suggested_create"}:
            return {"automation": self.store.create(workspace, thread_id, arguments), "created": True}
        if mode in {"update", "suggested_update"}:
            if not automation_id:
                raise AutomationValidationError("automationId é obrigatório")
            return {"automation": self.store.update(workspace, automation_id, arguments), "updated": True}
        if mode == "delete":
            if not automation_id:
                raise AutomationValidationError("automationId é obrigatório")
            if not self.store.delete(workspace, automation_id):
                raise AutomationValidationError("agendamento não encontrado")
            return {"automationId": automation_id, "deleted": True}
        raise AutomationValidationError("mode inválido")

    async def scheduler(self, runner: Callable[[dict[str, Any]], Awaitable[str]], interval: float = 15.0) -> None:
        while True:
            try:
                due = await asyncio.to_thread(self.store.claim_due)
                for automation in due:
                    run_id = await asyncio.to_thread(self.store.start_run, automation["id"])
                    try:
                        thread_id = await runner(automation)
                    except Exception as exc:
                        LOGGER.exception("Execução agendada %s falhou", automation["id"])
                        await asyncio.to_thread(self.store.finish_run, run_id, "", str(exc))
                    else:
                        await asyncio.to_thread(self.store.finish_run, run_id, thread_id, "")
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                LOGGER.exception("Agendador do Dex terminou um ciclo degradado: %s", exc)
            await asyncio.sleep(interval)


def migrate_thread_dynamic_tools(database: Path) -> int:
    """Attach automation_update to persisted app-server threads idempotently."""
    database = Path(database)
    if not database.is_file():
        return 0
    input_schema = json.dumps(AUTOMATION_INPUT_SCHEMA, ensure_ascii=False, separators=(",", ":"))
    changed = 0
    with sqlite3.connect(database, timeout=30) as connection:
        tables = {
            str(row[0])
            for row in connection.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall()
        }
        if not {"threads", "thread_dynamic_tools"}.issubset(tables):
            return 0
        thread_ids = [str(row[0]) for row in connection.execute("SELECT id FROM threads").fetchall()]
        for thread_id in thread_ids:
            existing = connection.execute(
                "SELECT position FROM thread_dynamic_tools WHERE thread_id=? AND name=?",
                (thread_id, AUTOMATION_TOOL_SPEC["name"]),
            ).fetchone()
            if existing:
                connection.execute(
                    """UPDATE thread_dynamic_tools SET description=?,input_schema=?,defer_loading=0,namespace=NULL
                       WHERE thread_id=? AND name=?""",
                    (AUTOMATION_TOOL_DESCRIPTION, input_schema, thread_id, AUTOMATION_TOOL_SPEC["name"]),
                )
                continue
            position = connection.execute(
                "SELECT COALESCE(MAX(position),-1)+1 FROM thread_dynamic_tools WHERE thread_id=?", (thread_id,)
            ).fetchone()[0]
            connection.execute(
                """INSERT INTO thread_dynamic_tools
                   (thread_id,position,name,description,input_schema,defer_loading,namespace)
                   VALUES (?,?,?,?,?,0,NULL)""",
                (thread_id, position, AUTOMATION_TOOL_SPEC["name"], AUTOMATION_TOOL_DESCRIPTION, input_schema),
            )
            changed += 1
    return changed
