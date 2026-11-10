"""Capture capability from native image bytes, never session authentication."""
import base64
import hashlib
import io
from PIL import Image
from .feedback_native_receipts import fresh,wire


def decoded(image,lease,stamp):
    if not isinstance(image,str) or not image.startswith('data:image/jpeg;base64,') or len(image)>1300000:
        raise ValueError('native capture envelope')
    raw=base64.b64decode(image.split(',',1)[1],validate=True)
    with Image.open(io.BytesIO(raw)) as im:
        width,height=im.size
        if im.format!='JPEG' or min(width,height)<1 or max(width,height)>8192 or width*height>16000000:
            raise ValueError('native capture dimensions')
        im.load()  # Full decoding, not merely a claimed MIME/header.
    return dict(lease=lease,observed_at=stamp,width=width,height=height,
                image_sha256=hashlib.sha256(raw).hexdigest())


def observation(binding,proofs,lease,now):
    # Both physical orientations must have fresh decoded captures in this turn.
    if not all(k in proofs for k in ('portrait','landscape')):return None
    for orientation,p in proofs.items():
        if (p['lease']!=lease or not fresh(p['observed_at'],now)
                or orientation!=('portrait' if p['height']>=p['width'] else 'landscape')):
            return None
    material=dict(binding=binding,lease=lease,proofs=proofs,reader='system.android.native_capture.v1')
    revision=hashlib.sha256(wire(material).encode()).hexdigest()
    return dict(id='android_capture',kind='capability',reader_id='system.android.native_capture.v1',
        subject=dict(event_id=binding['event_id'],source_report_id=binding['source_report_id'],
            contract_revision=binding['contract_revision'],origin='https://nc.sasocq.com',
            module=binding['module'],device='android'),revision=revision,
        observed_at=min(p['observed_at'] for p in proofs.values()),
        evidence_ref='urn:sasocq:native-capture:'+revision,satisfied=True,
        reason='decoded_native_portrait_landscape_captures')
