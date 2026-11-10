"""Conversation reservations for the host-side emulator provider.

Use a service-private database. Owner and purpose originate in the authenticated
Dex dispatcher. A slot stays occupied until the host confirms process exit.
"""
import secrets
import sqlite3
from contextlib import contextmanager
from dataclasses import dataclass


class Unavailable(RuntimeError):
    pass


@dataclass(frozen=True)
class Lease:
    slot: int
    workspace: str
    thread: str
    purpose: str
    token: str
    phase: str


class Reservations:
    def __init__(self, database):
        self.database = database
        with self.transaction() as db:
            db.execute("""CREATE TABLE IF NOT EXISTS leases (
                slot INTEGER PRIMARY KEY CHECK(slot IN (0,1)),
                workspace TEXT NOT NULL, thread TEXT NOT NULL,
                purpose TEXT NOT NULL UNIQUE, token TEXT NOT NULL UNIQUE,
                phase TEXT NOT NULL CHECK(phase IN ('starting','ready','stopping')),
                UNIQUE(workspace,thread))""")

    def list(self):
        with self.transaction() as db:
            return tuple(Lease(*row) for row in db.execute("SELECT * FROM leases ORDER BY slot"))

    @contextmanager
    def transaction(self):
        db = sqlite3.connect(self.database, timeout=4)
        try:
            with db:
                db.execute("BEGIN IMMEDIATE")
                yield db
        finally:
            db.close()

    def reserve(self, workspace, thread, purpose, admitted):
        if not all(isinstance(v,str) and 0 < len(v) <= 256 for v in (workspace,thread,purpose)):
            raise ValueError("Invalid reservation identity")
        with self.transaction() as db:
            rows = [Lease(*r) for r in db.execute("SELECT * FROM leases")]
            for row in rows:
                if (row.workspace,row.thread)==(workspace,thread):
                    if row.purpose==purpose and row.phase!='stopping':
                        return row
                    raise Unavailable("Conversation already owns a reservation")
                if row.purpose==purpose:
                    raise Unavailable("Purpose in use")
            available = sorted({0,1} - {r.slot for r in rows})
            if not available or not admitted(tuple(rows)):
                raise Unavailable("Insufficient capacity")
            row = Lease(available[0],workspace,thread,purpose,secrets.token_hex(24),'starting')
            db.execute("INSERT INTO leases VALUES (?,?,?,?,?,?)",
                       (row.slot,row.workspace,row.thread,row.purpose,row.token,row.phase))
            return row

    def transition(self, lease, target):
        with self.transaction() as db:
            record=db.execute("SELECT phase FROM leases WHERE slot=? AND workspace=? AND thread=? AND token=?",
                              (lease.slot,lease.workspace,lease.thread,lease.token)).fetchone()
            if not record:
                raise PermissionError("Reservation has changed")
            allowed={'starting':{'ready','stopping'},'ready':{'stopping'},'stopping':set()}
            if target not in allowed[record[0]]:
                raise ValueError("Invalid reservation transition")
            db.execute("UPDATE leases SET phase=? WHERE token=?",(target,lease.token))

    def release_after_exit(self, lease, verify_stopped):
        with self.transaction() as db:
            row=db.execute("SELECT * FROM leases WHERE slot=?",(lease.slot,)).fetchone()
            if not row:
                return
            current=Lease(*row)
            if (current.workspace,current.thread,current.token)!=(lease.workspace,lease.thread,lease.token):
                raise PermissionError("Reservation has changed")
            if current.phase!='stopping' or not verify_stopped(current):
                raise Unavailable("Host has not confirmed shutdown")
            db.execute("DELETE FROM leases WHERE token=?",(lease.token,))
