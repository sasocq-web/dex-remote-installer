"""Durable adapter for Codex's async AgentMessage questions, not RPC approvals."""
import json, sqlite3, time
from pathlib import Path
from contextlib import closing


def question_request(workspace, thread, turn, item):
    if item.get('type') != 'agentMessage' or item.get('delivery') != 'async' or not item.get('questions') or not item.get('id') or not thread:
        return None
    questions=[]
    for index,q in enumerate(item['questions'][:3]):
        if not isinstance(q,dict) or not isinstance(q.get('title'),str):return None
        options=[{'label':v,'description':''} for v in (q.get('options') or []) if isinstance(v,str)]
        questions.append({'id':str(index),'header':'Pergunta do Codex','question':q['title'],'options':options,'isOther':True})
    if not questions:return None
    return {'id':f'async:{thread}:{item["id"]}', 'method':'item/tool/requestUserInput',
            'params':{'threadId':thread,'turnId':turn,'itemId':item['id'],'isBlocking':False,'asyncMessage':True,'questions':questions}}


class AsyncQuestions:
    def __init__(self,path):
        self.path=Path(path);self.path.parent.mkdir(parents=True,exist_ok=True)
        with closing(self.connect()) as db:
            db.execute('CREATE TABLE IF NOT EXISTS questions (workspace TEXT, id TEXT, request TEXT, status TEXT, created REAL, PRIMARY KEY(workspace,id))');db.commit()
        self.path.chmod(0o600)
    def connect(self):return sqlite3.connect(self.path,timeout=10)
    def record(self,workspace,thread,turn,item):
        request=question_request(workspace,thread,turn,item)
        if not request:return None
        with closing(self.connect()) as db:
            with db:db.execute('INSERT OR IGNORE INTO questions VALUES (?,?,?,?,?)',(workspace,request['id'],json.dumps(request),'pending',time.time()))
            row=db.execute('SELECT status,created FROM questions WHERE workspace=? AND id=?',(workspace,request['id'])).fetchone()
        if row[0]!='pending' or row[1]<time.time()-3600:return None
        return {'kind':'server_request','workspace':workspace,'request':request}
    def pending(self):
        with closing(self.connect()) as db:
            return [{'kind':'server_request','workspace':w,'request':json.loads(r)} for w,r in db.execute('SELECT workspace,request FROM questions WHERE status=? AND created>?',('pending',time.time()-3600))]
    def claim(self,workspace,key,answers):
        with closing(self.connect()) as db:
            db.execute('BEGIN IMMEDIATE')
            row=db.execute('SELECT request,status,created FROM questions WHERE workspace=? AND id=?',(workspace,key)).fetchone()
            if not row or row[1]!='pending' or row[2]<time.time()-3600:raise ValueError('Pergunta já respondida, expirada ou com entrega em andamento. Não repita uma entrega incerta.')
            request=json.loads(row[0]);questions=request['params']['questions']
            if not isinstance(answers,dict) or answers and set(answers)!={q['id'] for q in questions}:raise ValueError('Responda todas as perguntas.')
            lines=[]
            for q in questions:
                if not answers:break
                answer=answers[q['id']]
                values=answer.get('answers') if isinstance(answer,dict) else None
                if not isinstance(values,list) or not values or any(not isinstance(v,str) or not v.strip() or len(v)>8192 for v in values):raise ValueError('Resposta inválida.')
                lines.append(q['question']+'\nResposta: '+'; '.join(values))
            db.execute('UPDATE questions SET status=? WHERE workspace=? AND id=?',('sending' if answers else 'cancelled',workspace,key));db.commit()
        return request,'Respostas à pergunta '+request['params']['itemId']+':\n\n'+'\n\n'.join(lines)
    def finish(self,workspace,key):
        with closing(self.connect()) as db:
            with db:db.execute('UPDATE questions SET status=? WHERE workspace=? AND id=?',('answered',workspace,key))
