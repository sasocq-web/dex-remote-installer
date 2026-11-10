"""Protected HTTP challenge transport. Secrets never enter MCP arguments/traces."""
import asyncio
import json
import re
from pathlib import Path
from urllib.parse import urlsplit

ORIGIN = "https://vpn1.nuclep.gov.br"
WORKSPACES = {"system", "project:project-96921b3806e0"}

def validate_binding(workspace, payload):
    if workspace not in WORKSPACES or set(payload) != {"request_token", "target_id", "url"}:
        raise ValueError("http_auth_scope_rejected")
    u = urlsplit(payload["url"])
    if u.scheme != "https" or u.netloc != "vpn1.nuclep.gov.br" or u.fragment:
        raise ValueError("http_auth_scope_rejected")
    if not re.match(r"^/proxy/[a-fA-F0-9]{8,64}/https?/[a-zA-Z0-9.-]+/", u.path):
        raise ValueError("http_auth_scope_rejected")
    if not re.fullmatch(r"[A-Fa-f0-9]{32}", payload["target_id"]):
        raise ValueError("http_auth_scope_rejected")
    return ORIGIN

async def authenticate(payload, values):
    # Bound child, no shell, secret argv/env/file, stderr or MCP session trace.
    program = Path(__file__).resolve().parents[1] / "system/http_auth.mjs"
    node = "/home/codex/.local/share/codex-linux-control/tools/node/bin/node"
    proc = None
    try:
        if set(values) != {"login", "password"} or any(not isinstance(v,str) or not v or len(v)>8192 for v in values.values()):
            raise ValueError()
        proc = await asyncio.create_subprocess_exec(node, str(program), stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.DEVNULL)
        raw = json.dumps({"url":payload["url"],"target_id":payload["target_id"],"values":values}).encode()
        output, _ = await asyncio.wait_for(proc.communicate(raw), 28)
        result = json.loads(output)
        if proc.returncode or not isinstance(result,dict): raise ValueError()
        # Never pass arbitrary child fields or errors back to the agent.
        return {"ok":True, "http_auth_attempted": result.get("attempted") is True,
            "authenticated": result.get("authenticated") is True,
            "document_ready": result.get("document_ready") is True,
            "scheme": result.get("scheme") if result.get("scheme") in {"basic","digest","ntlm","negotiate"} else "unknown",
            "http_status":result.get("status") if type(result.get("status")) is int and 100<=result["status"]<=599 else None,
            "auth_callback_received":result.get("auth_event") is True,
            "reason":result.get("reason") if result.get("reason") in {"authenticated","authentication_not_completed","challenge_outside_binding","http_challenge_without_auth_callback","browser_network_auth_error","no_response_observed"} else "invalid_receipt"}
    except Exception:
        raise RuntimeError("protected_http_auth_failed") from None
    finally:
        values.clear()
        if proc and proc.returncode is None:
            proc.kill()
            await proc.wait()
