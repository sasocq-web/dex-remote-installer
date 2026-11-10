"""Credential fills into one explicitly bound production service browser."""
from __future__ import annotations
import base64
import json
from pathlib import Path
import re
from .control_plane import async_request

SERVICE_BROWSER_TOOL_SPEC = {
    'type':'function','name':'service_browser_control','deferLoading':False,
    'description':'Canal protegido do cofre Dex para o navegador próprio MeuTH. Restrito ao Sistema e ao projeto Power automate, ao container de autenticação identificado e à origem HTTPS exata https://vpn1.nuclep.gov.br. status retorna somente prontidão; fill_saved reutiliza login/senha do cofre sem pedir novamente, sem devolver valores e sem submeter login. Exige sessão ativa com prazo de 900 segundos, coletor parado e trava pertencente à sessão; não inicia navegador ou consumidor, não remove travas, não copia perfil/cookies e não abre listener. request_otp abre Código temporário na própria conversa somente quando o usuário explicitamente autorizou login/MFA. Vinculado à sessão e prazo, não salva OTP nem submete formulário; após preenchimento use o fluxo autorizado do próprio serviço.',
    'inputSchema':{'type':'object','additionalProperties':False,'properties':{
        'operation':{'type':'string','enum':['status','fill_saved','request_otp']},
        'target':{'type':'string','enum':['meuth']},
        'prompt_if_missing':{'type':'boolean','default':False,'description':'true somente após pedido explícito de login/MFA do usuário; obrigatório para request_otp'}
    },'required':['operation','target']}
}

class ServiceBrowserLifecycle:
    def __init__(self, broker=async_request):
        self.broker=broker

    async def execute(self, workspace, thread, args):
        if workspace not in {'system','project:project-96921b3806e0'}:
            raise ValueError('Workspace não vinculado ao navegador MeuTH')
        if not isinstance(thread,str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,200}',thread):
            raise ValueError('Conversa inválida')
        if not isinstance(args,dict) or set(args) not in ({'operation','target'},{'operation','target','prompt_if_missing'}) or args['target']!='meuth' or args['operation'] not in {'status','fill_saved','request_otp'}:
            raise ValueError('Destino ou operação inválidos')
        if args['operation']=='request_otp' and args.get('prompt_if_missing') is not True:
            raise ValueError('Entrada MFA exige login explicitamente autorizado e prompt_if_missing=true')
        if args['operation']!='request_otp' and args.get('prompt_if_missing'):
            raise ValueError('Prompt não permitido nesta operação')
        payload=base64.b64encode(json.dumps({'workspace':workspace,'thread':thread,'operation':'otp_probe' if args['operation']=='request_otp' else args['operation']}).encode()).decode()
        helper=Path(__file__).resolve().parents[1]/'system/meuth_service.py'
        response=await self.broker('host-admin',{'operation':'exec','argv':['/usr/bin/python3',str(helper),payload],'timeout':90},timeout=100)
        raw=response.get('result',{}).get('output',{})
        if not isinstance(raw,dict) or not isinstance(raw.get('returncode'),int):
            raise RuntimeError('Transporte protegido indisponível')
        try:
            result=json.loads(raw.get('output','{}'))
        except (ValueError,TypeError):
            raise RuntimeError('Resposta protegida inválida') from None
        if not isinstance(result,dict):
            raise RuntimeError('Resposta protegida inválida')
        if not response.get('ok') or raw.get('returncode') or result.get('ok') is not True:
            safe_errors={'session_guard_or_browser_not_ready','protected_transport_failed','invalid_protected_response','saved_credentials_unavailable','login_fields_not_ready','mfa_required','protected_fill_not_verified','production_unavailable','protected_service_failed'}
            reason=result.get('error')
            if not isinstance(reason,str) or reason not in safe_errors:
                reason='service_failed'
            raise RuntimeError('Canal protegido recusou a operação: '+reason)
        if args['operation']=='request_otp':
            from .main import _register_service_otp_request
            return await _register_service_otp_request(workspace,thread,result)
        return result

    async def fill_otp(self,record,content):
        helper=Path(__file__).resolve().parents[1]/'system/meuth_service.py'
        payload={'workspace':record['workspace'],'thread':record['thread_id'],'operation':'fill_otp','session_id':record['service_session_id'],'expires_at':record['service_expires_at'],'values':content}
        try:
            response=await self.broker('host-admin',{'operation':'exec','argv':['/usr/bin/python3',str(helper),'--protected-stdin'],'input_text':json.dumps(payload),'timeout':60},timeout=70)
            raw=response.get('result',{}).get('output',{})
            answer=json.loads(raw.get('output','{}'))
            if not response.get('ok') or raw.get('returncode') or answer.get('filled') is not True:
                raise RuntimeError('Sessão MFA expirada ou destino protegido indisponível')
            return {'filled':True,'submitted':False}
        finally:
            content.clear()
            payload.clear()

    async def handle_server_request(self,workspace,message):
        params=message.get('params') or {}
        if message.get('method')!='item/tool/call' or params.get('tool')!='service_browser_control': return None
        try:
            result=await self.execute(workspace,params.get('threadId',''),params.get('arguments') or {})
            return {'success':True,'contentItems':[{'type':'inputText','text':json.dumps(result,ensure_ascii=False)}]}
        except Exception as exc:
            return {'success':False,'contentItems':[{'type':'inputText','text':str(exc)}]}
