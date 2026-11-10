"""Candidate private MCP byte transport. No browser creation or public JSON facts.

The native lifecycle must inject its own bound MCP call/context check. Returning
bytes here is NOT a receipt; SourceReceiptStore and current contract guards follow.
"""
import asyncio,base64,hashlib,json,re,time
from pathlib import Path
from .feedback_source_receipts import ORIGIN,hex64

LIMIT=16000000
CHUNK=48000

def unique(pairs):
    result={}
    for key,value in pairs:
        if key in result:raise ValueError('duplicate private JSON field')
        result[key]=value
    return result

def tool_value(reply):
    for _ in range(3):
        if not isinstance(reply,dict) or reply.get('isError') or reply.get('error'):
            raise ValueError('managed source MCP failure')
        if 'content' in reply:break
        reply=reply.get('result')
    content=reply.get('content') if isinstance(reply,dict) else None
    if not isinstance(content,list):raise ValueError('managed source MCP envelope')
    text='\n'.join(x.get('text','') for x in content if isinstance(x,dict) and x.get('type')=='text')
    if len(text)>65536 or not text.startswith('### Result\n'):
        raise ValueError('managed source MCP result format')
    try:
        value,_=json.JSONDecoder(object_pairs_hook=unique).raw_decode(text[len('### Result\n'):].lstrip())
    except (ValueError,TypeError) as exc:raise ValueError('managed source MCP JSON') from exc
    return value

class SourceMcpCollector:
    def __init__(self,bundle,pins,call,context_current):
        self.bundle=Path(bundle);self.pins=dict(pins);self.call=call;self.current=context_current
        self.source=self.checked('collect-fixed-source.js')
        self.registry=json.loads(self.checked('registry.json'),object_pairs_hook=unique)
        if self.registry.get('origin')!=ORIGIN:raise ValueError('fixed source origin')
    def checked(self,name):
        p=self.bundle/name
        if p.is_symlink() or not p.is_file() or p.stat().st_size>1000000:
            raise ValueError('immutable source bundle')
        raw=p.read_bytes()
        if not hex64(self.pins.get(name)) or hashlib.sha256(raw).hexdigest()!=self.pins[name]:
            raise ValueError('unreviewed source bundle')
        return raw.decode('utf8')
    async def capture(self,request,nonce):
        if (not isinstance(request,dict) or set(request)!={'event_id','dependency_id'}
            or not hex64(request['event_id']) or not isinstance(request['dependency_id'],str)
            or not self.registry['bindings'].get(request['event_id'],{}).get(request['dependency_id'])
            or not hex64(nonce)):
            raise ValueError('fixed source request IDs/nonce')
        self.checked('collect-fixed-source.js');self.checked('registry.json')
        deadline=time.monotonic()+150;key='__sasocq_canonical_'+nonce
        key_js=json.dumps(key);nonce_js=json.dumps(nonce);origin_js=json.dumps(ORIGIN)
        async def evaluate(function):
            if self.current() is not True:raise ValueError('own active source context required')
            remaining=deadline-time.monotonic()
            if remaining<=0:raise TimeoutError('source transfer global deadline')
            try:
                reply=await asyncio.wait_for(self.call('browser_evaluate',{'function':function}),remaining)
            except asyncio.CancelledError:raise
            except Exception:raise RuntimeError('managed source MCP unavailable') from None
            if self.current() is not True:raise ValueError('source context changed during MCP call')
            return tool_value(reply)
        start=("async () => { const module=undefined; "+self.source+";"
            +"if(location.origin!=="+origin_js+")throw Error('wrong_origin');"
            +"const raw=JSON.stringify(await canonicalSourceFactory().collect("+json.dumps(request)+","+nonce_js+"));"
            +"const bytes=new TextEncoder().encode(raw);if(bytes.length>16000000)throw Error('capture_capacity');"
            +"let binary='';for(let i=0;i<bytes.length;i+=8192)binary+=String.fromCharCode(...bytes.slice(i,i+8192));"
            +"const encoded=btoa(binary);window["+key_js+"]={encoded};"
            +"const sha256=Array.from(new Uint8Array(await crypto.subtle.digest('SHA-256',bytes)),x=>x.toString(16).padStart(2,'0')).join('');"
            +"return {schema:'canonical-source-transfer-v1',nonce:"+nonce_js+",origin:location.origin,byte_length:bytes.length,encoded_length:encoded.length,sha256}; }")
        try:
            descriptor=await evaluate(start)
            if (not isinstance(descriptor,dict) or set(descriptor)!={'schema','nonce','origin','byte_length','encoded_length','sha256'}
                or descriptor['schema']!='canonical-source-transfer-v1' or descriptor['nonce']!=nonce
                or descriptor['origin']!=ORIGIN or type(descriptor['byte_length']) is not int
                or not 0<descriptor['byte_length']<=LIMIT or type(descriptor['encoded_length']) is not int
                or descriptor['encoded_length']!=((descriptor['byte_length']+2)//3)*4
                or not hex64(descriptor['sha256'])):raise ValueError('source transfer descriptor')
            parts=[]
            for index,offset in enumerate(range(0,descriptor['encoded_length'],CHUNK)):
                function=("() => {if(location.origin!=="+origin_js+")throw Error('wrong_origin');"
                    +"const v=window["+key_js+"];if(!v)throw Error('capture_missing');return {nonce:"
                    +nonce_js+",index:"+str(index)+",data:v.encoded.slice("+str(offset)+","+str(offset+CHUNK)+")};}")
                part=await evaluate(function)
                wanted=min(CHUNK,descriptor['encoded_length']-offset)
                if (not isinstance(part,dict) or set(part)!={'nonce','index','data'}
                    or part['nonce']!=nonce or type(part['index']) is not int or part['index']!=index
                    or not isinstance(part['data'],str) or len(part['data'])!=wanted
                    or re.fullmatch('[A-Za-z0-9+/=]*',part['data']) is None):raise ValueError('source transfer chunk')
                parts.append(part['data'])
            raw=base64.b64decode(''.join(parts),validate=True)
            if len(raw)!=descriptor['byte_length'] or hashlib.sha256(raw).hexdigest()!=descriptor['sha256']:
                raise ValueError('source transfer digest')
            value=json.loads(raw.decode('utf8'),object_pairs_hook=unique)
            if not isinstance(value,dict) or value.get('nonce')!=nonce or any(value.get(k)!=v for k,v in request.items()):
                raise ValueError('source transferred capture binding')
            return value
        finally:
            # If the context changed, never send cleanup to a different page.
            if self.current() is True:
                try:
                    await asyncio.wait_for(self.call('browser_evaluate',{'function':
                        "() => {if(location.origin!=="+origin_js+")return false;delete window["+key_js+"];return true;}"}),5)
                except (Exception,asyncio.CancelledError):pass
