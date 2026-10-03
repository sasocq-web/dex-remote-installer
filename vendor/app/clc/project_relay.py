"""Idempotent Dot requests to the existing Dex project executor, never another writer."""
import hashlib
import json
import os
from pathlib import Path
import re
import sqlite3
import time
import uuid

class ProjectRelay:
    def __init__(self, directory, handler, owner_uid, worker_uid):
        self.handler, self.owner_uid, self.worker_uid = handler, owner_uid, worker_uid
        self.db = sqlite3.connect(Path(directory) / 'project-relay.sqlite3')
        self.db.row_factory = sqlite3.Row
        self.db.execute('CREATE TABLE IF NOT EXISTS requests (id TEXT PRIMARY KEY, request_key TEXT UNIQUE, digest TEXT, payload TEXT, status TEXT, result TEXT, created REAL)')
        self.db.execute("UPDATE requests SET status='dispatch_uncertain' WHERE status='dispatching'")
        self.db.commit()
        os.chmod(Path(directory) / 'project-relay.sqlite3', 0o600)

    def public(self, row):
        return dict(id=row['id'], key=row['request_key'], status=row['status'], result=json.loads(row['result']))

    async def handle(self, uid, data):
        if uid not in (self.owner_uid, self.worker_uid):
            raise PermissionError('identidade não autorizada')
        op = data.get('operation')
        fields = {'project-list': {'operation'}, 'project-read': {'operation','project_id','thread_id'}, 'project-submit': {'operation','key','project_id','thread_id','message'}, 'project-result': {'operation','id'}}
        if op not in fields or set(data) - fields[op]:
            raise ValueError('operação ou campos não permitidos')
        if op == 'project-result':
            row = self.db.execute('SELECT * FROM requests WHERE id=?', (str(data.get('id','')),)).fetchone()
            if row is None: raise ValueError('pedido não encontrado')
            return self.public(row)
        if op == 'project-list': return await self.handler('list', {})
        project = data.get('project_id')
        thread = data.get('thread_id', '')
        if not isinstance(project,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,100}',project): raise ValueError('projeto inválido')
        if not isinstance(thread,str) or (thread and not re.fullmatch(r'[a-f0-9-]{36}',thread)): raise ValueError('conversa inválida')
        payload = dict(project_id=project, thread_id=thread)
        if op == 'project-read':
            if not thread: raise ValueError('conversa obrigatória')
            return await self.handler('read',payload)
        key, message = data.get('key'), data.get('message')
        if not isinstance(key,str) or not re.fullmatch(r'[A-Za-z0-9_.-]{8,100}',key): raise ValueError('chave inválida')
        if not isinstance(message,str) or not 1 <= len(message.strip()) <= 6000: raise ValueError('mensagem deve ter 1..6000 caracteres')
        payload['message'] = message
        raw = json.dumps(payload,sort_keys=True,ensure_ascii=False)
        digest = hashlib.sha256(raw.encode()).hexdigest()
        old = self.db.execute('SELECT * FROM requests WHERE request_key=?',(key,)).fetchone()
        if old:
            if old['digest'] != digest: raise ValueError('chave já usada com outro conteúdo')
            return self.public(old)
        await self.handler('validate',payload)
        if self.db.execute("SELECT count(*) FROM requests WHERE status IN ('queued','dispatching','dispatch_uncertain')").fetchone()[0] >= 5:
            raise ValueError('fila cheia; confira os pedidos existentes')
        rid = uuid.uuid4().hex
        self.db.execute('INSERT INTO requests VALUES (?,?,?,?,?,?,?)',(rid,key,digest,raw,'queued','{}',time.time()))
        self.db.commit()
        return self.public(self.db.execute('SELECT * FROM requests WHERE id=?',(rid,)).fetchone())

    async def dispatch_one(self):
        row = self.db.execute("SELECT * FROM requests WHERE status='queued' ORDER BY created LIMIT 1").fetchone()
        if row is None: return
        payload=json.loads(row['payload'])
        try:
            await self.handler('validate',payload)
        except Exception:
            self.db.execute("UPDATE requests SET status='rejected',result=? WHERE id=?",(json.dumps({'error':'Projeto/conversa indisponível; nenhuma mensagem enviada.'}),row['id']))
            self.db.commit()
            return
        self.db.execute("UPDATE requests SET status='dispatching' WHERE id=?",(row['id'],))
        self.db.commit()
        try:
            result=await self.handler('send',payload)
            status='dispatched'
        except Exception:
            result={'error':'Entrega não confirmada. Não repetir com outra chave; revisar no Sistema.'}
            status='dispatch_uncertain'
        self.db.execute('UPDATE requests SET status=?,result=? WHERE id=?',(status,json.dumps(result,ensure_ascii=False),row['id']))
        self.db.commit()
