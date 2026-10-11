"""Read-only Hamle stop intake, before routine scheduled work; no grants/sends.

Only existing active V11 authority-queue work is considered. Missing role catalog
causes a real received stop to revoke/hold, never disappear into ordinary review.
"""
from dataclasses import dataclass
from datetime import datetime, timezone
import re
import unicodedata

from cloud_store import StoreError
from v11_work_authority import HAMLE, ISSUERS
from v11_work_authority_store import WorkAuthorityStore, TrustedWorkStop


@dataclass(frozen=True)
class ReceivedWorkMail:
    thread_id: str
    message_id: str
    sender: str
    direct_to: tuple
    direct_cc: tuple
    fingerprint: str
    captured_at: str
    text: str
    provider_identity: str = HAMLE
    automatic: bool = False
    actual_latest_incoming: bool = True
    unseen_prior_incoming: bool = False


def stop_intent(text):
    """Inspect current authored text only; never a quoted historical instruction.

    Broad stop/takeover wording safely holds when semantics are uncertain. This
    does not claim human-level linguistic understanding or treat it as a grant.
    """
    if not isinstance(text, str) or len(text.encode()) > 12000:
        raise StoreError('v11_stop_text_unavailable')
    lines = []
    for line in text.splitlines():
        clean = line.strip()
        folded = clean.casefold()
        if clean.startswith('>') or re.match(r'^(from:|kimden:|on .+wrote:|.*şöyle yazdı:|[-_]{3,})', folded):
            break
        lines.append(clean)
    current = '\n'.join(lines)
    # Turkish dotted I normalization; retain word boundaries.
    current = unicodedata.normalize('NFKC', current).casefold().replace('i\u0307','i')
    if re.search(r'\b(devral\w*|durdur\w*)\b|\bhamle\s*[,!:]?\s+dur\b', current):
        return 'human_takeover' if re.search(r'\bdevral\w*\b', current) else 'explicit_stop'
    return None


class HamleStopReader:
    """Existing verified Gmail helper, same Hamle secret, exact thread reads only."""
    def __init__(self, gmail_store):
        self.gmail_store = gmail_store

    def read_latest(self, work, grant):
        import cloud_providers as provider
        helper, reader, normalizer, intake = provider._modules()
        thread = work['thread_id']
        if not re.fullmatch(r'[0-9a-f]{1,64}', thread):
            raise StoreError('v11_stop_thread_invalid')
        session = provider._session()
        try:
            def read(mid):
                return reader._read_verified(thread, mid, helper=helper, normalizer=normalizer,
                    store=self.gmail_store, service_factory=provider._gmail_service,
                    request_factory=lambda: provider._GoogleRequest(session, gmail=True))
            first = read(None)
            if first.get('email') != HAMLE or first.get('mailbox_writes') != 0:
                raise StoreError('v11_stop_mailbox_unverified')
            metadata = intake.metadata(first['observation']); latest = metadata['latest']
            origin = grant['binding']['source_message_id'] if grant else None
            dates = {m['id']:m['time_ms'] for m in metadata['messages']}
            unreviewed = [m for m in metadata['messages'] if m['direction']=='inbound'
                          and not m['automated'] and m['id']!=origin
                          and (origin not in dates or m['time_ms']>dates[origin])]
            if latest is None or latest['automated'] or latest['from'] == HAMLE or HAMLE not in latest['to']+latest['cc']:
                # An automatic or no-To/CC latest message must never conceal a
                # real incoming stop that arrived after our bound source.
                if unreviewed:
                    raise StoreError('v11_stop_prior_messages_unreviewed')
                return None
            fingerprint = intake.snapshot_fingerprint(first['observation'])
            body = read(latest['id'])
            if body.get('email') != HAMLE or intake.snapshot_fingerprint(body['observation']) != fingerprint:
                raise StoreError('v11_stop_thread_changed')
            row = next(m for m in body['observation']['messages'] if m['message_id']==latest['id'])
            plain = [p['text'] for p in row['body'] if p['mime_type']=='text/plain']
            if row['body_status']!='decoded_unrendered' or row['issues'] or not row['envelope_eligible'] or len(plain)!=1:
                raise StoreError('v11_stop_text_unavailable')
            # The existing reader intentionally decodes only latest incoming.
            # Multiple unreviewed incoming messages must not hide an older stop.
            return ReceivedWorkMail(thread, latest['id'], latest['from'], tuple(latest['to']),tuple(latest['cc']),
                fingerprint,datetime.now(timezone.utc).isoformat(),plain[0],unseen_prior_incoming=len(unreviewed)>1)
        finally:
            session.close()


def run_stop_intake(operations, reader, *, clock=None):
    """Bounded existing-work sweep. Any uncertainty blocks this scheduled slot.

    With current live scopes disabled, customer stop uses durable pending hold.
    """
    now = clock or (lambda:datetime.now(timezone.utc))
    store = WorkAuthorityStore(operations); queue = store.queue()
    works=[]; cursor=None
    for _ in range(4):
        page=queue.page(kind='work',limit=25,cursor=cursor)
        works.extend(page['items']);cursor=page['next_cursor']
        if cursor is None:break
    if cursor is not None:raise StoreError('v11_stop_sweep_incomplete')
    stopped=[];checked=0
    for hint in works:
        current=store.observe_work(hint['work_id']);work=current['work'];record=current['record']
        if work['mode']!='active':continue
        grant=record.get('grant') if record else None
        try:mail=reader.read_latest(work,grant)
        except Exception:raise StoreError('v11_stop_intake_read_failed') from None
        checked+=1
        if mail is None:continue
        moment=now()
        if (type(mail) is not ReceivedWorkMail or mail.thread_id!=work['thread_id']
            or mail.provider_identity!=HAMLE or mail.automatic or not mail.actual_latest_incoming
            or mail.sender==HAMLE or HAMLE not in mail.direct_to+mail.direct_cc
            or not 0<=(moment-datetime.fromisoformat(mail.captured_at)).total_seconds()<=60):
            raise StoreError('v11_stop_intake_untrusted')
        intent=stop_intent(mail.text)
        if not intent:
            if mail.unseen_prior_incoming:raise StoreError('v11_stop_prior_messages_unreviewed')
            continue
        receipt=TrustedWorkStop(work_id=work['work_id'],actor=mail.sender,
            identity_subject='verified-hamle-gmail:'+mail.message_id,
            evidence_ref='gmail-visible-stop:'+mail.thread_id+':'+mail.message_id+':'+mail.fingerprint,
            observed_at=moment.isoformat(),channel='authenticated_gmail_api',thread_id=mail.thread_id,
            actual_to_cc=mail.direct_to+mail.direct_cc,provider_identity=HAMLE,
            source_message_id=mail.message_id,source_fingerprint=mail.fingerprint,source_sender=mail.sender,stop_intent=intent)
        if mail.sender in ISSUERS:
            result=store.stop_work(work['work_id'],receipt,expected_work_generation=work['work_generation'])
        else:
            # Scope installation is currently closed. Stop still takes effect.
            # Never make a customer or model appear to be a new agency issuer.
            result=store.hold_received_stop(work['work_id'],receipt,expected_work_generation=work['work_generation'])
        stopped.append({'work_id':work['work_id'],'message_id':mail.message_id,
                        'verification_pending':result['record']['last_stop']['verification_pending']})
    return {'status':'completed','checked_work_count':checked,'stops':stopped,
            'model_calls':0,'business_writes':0,'grants_created':0,'customer_live_enabled':False}
