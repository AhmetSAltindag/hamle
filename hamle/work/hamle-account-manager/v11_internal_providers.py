"""Narrow, single-attempt real providers for the new internal production proof.

No WAT/customer writes; no implicit durable state or action authority. Caller
owns before_post fences and persists every receipt before the next operation.
"""
from __future__ import annotations
import base64
from email import policy
from email.message import EmailMessage
from email.parser import BytesParser
from email.utils import getaddresses
import hashlib
from html import escape
from html.parser import HTMLParser
import re
from urllib.parse import quote, urlsplit

import basecamp_cloud_auth
import business_writers as writers
import cloud_providers as provider
from recipient_policy import derive_reply_recipients

SUBJECT = provider.PRODUCTION_INTERNAL_SUBJECT
ACCOUNT = 5898865
PROJECT = 49184053
PARENT = 10384126975
HAMLE_ID = 53358100
OLD_TASKS = frozenset((10391849167, 10392247471))
BC = f"https://3.basecampapi.com/{ACCOUNT}"
PROJECT_BASE = f"{BC}/buckets/{PROJECT}"
MAX_FILE = 2 * 1024 * 1024
MAX_RESPONSE = 8 * 1024 * 1024


class InternalProviderError(writers.WriterError):
    pass


def require(ok, code):
    if not ok:
        raise InternalProviderError(code)


def _id(value):
    require((type(value) is int and value > 0) or
            (isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,29}", value)), "internal_identifier_invalid")
    return int(value)


def _file(name, data, mime):
    require(isinstance(name, str) and re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.png", name)
            and '..' not in name, "internal_filename_invalid")
    require(type(data) is bytes and 8 < len(data) <= MAX_FILE and data.startswith(b'\x89PNG\r\n\x1a\n')
            and mime == 'image/png', "internal_png_required")
    return {'filename': name, 'content_type': mime, 'byte_size': len(data), 'sha256': hashlib.sha256(data).hexdigest()}


def _receipt(value):
    require(isinstance(value, dict), "attachment_receipt_invalid")
    require(isinstance(value.get('attachable_sgid'), str)
            and re.fullmatch(r'[A-Za-z0-9_=+/-]{10,4096}', value['attachable_sgid']), "attachment_receipt_invalid")
    identifier = value.get('provider_id')
    if isinstance(identifier, str) and identifier.startswith('basecamp-sgid-sha256:'):
        require(identifier == 'basecamp-sgid-sha256:' + hashlib.sha256(value['attachable_sgid'].encode()).hexdigest()
                and value.get('identifier_kind') == 'sgid_digest', 'attachment_receipt_invalid')
    else:
        _id(identifier)
    require(isinstance(value.get('filename'), str) and re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_.-]{0,100}\.png', value['filename'])
            and '..' not in value['filename'] and value.get('content_type') == 'image/png'
            and type(value.get('byte_size')) is int and 8 < value['byte_size'] <= MAX_FILE
            and isinstance(value.get('sha256'), str) and re.fullmatch(r'[0-9a-f]{64}', value['sha256']), "attachment_receipt_invalid")
    return value


def _attachment_identifier(row):
    if 'id' in row:
        return {'provider_id': str(_id(row['id'])), 'identifier_kind': 'native_id'}
    sgid = row.get('attachable_sgid', row.get('sgid'))
    require(isinstance(sgid, str) and re.fullmatch(r'[A-Za-z0-9_=+/-]{10,4096}', sgid), 'attachment_receipt_invalid')
    return {'provider_id': 'basecamp-sgid-sha256:' + hashlib.sha256(sgid.encode()).hexdigest(), 'identifier_kind': 'sgid_digest'}


def _download_bytes(session, url, token):
    """One native Basecamp GET, at most one observed storage redirect.

    This transport was verified read-only against the old internal attachment.
    OAuth goes only to the exact Basecamp API/storage origin; signed storage
    redirects receive no Authorization header and never leave this function.
    """
    response = None
    try:
        headers = {'Authorization': 'Bearer ' + provider._token(token),
                   'User-Agent': 'Hamle Internal Attachment Verification (ahmet@gamechangers.com.tr)'}
        response = session.request('GET', url, headers=headers, timeout=(10, 30), allow_redirects=False, stream=True)
        if response.status_code == 302:
            location = response.headers.get('Location', '')
            parsed = urlsplit(location)
            require(parsed.scheme == 'https' and parsed.netloc == 'storage.basecamp.com'
                    and not parsed.fragment, 'attachment_redirect_forbidden')
            response.close(); response = None
            response = session.request('GET', location, headers={'User-Agent': headers['User-Agent']},
                                       timeout=(10, 30), allow_redirects=False, stream=True)
        require(response.status_code == 200, 'provider_response_unconfirmed')
        chunks, total = [], 0
        for chunk in response.iter_content(chunk_size=16384):
            total += len(chunk)
            require(total <= MAX_FILE, 'provider_response_too_large')
            chunks.append(chunk)
        return b''.join(chunks)
    except InternalProviderError:
        raise
    except Exception:
        raise InternalProviderError('provider_outcome_unknown_no_retry') from None
    finally:
        if response is not None:
            try: response.close()
            except Exception: pass


def _transport(session, method, url, token=None, *, data=None, expected=200, raw=False, headers=None, limit=MAX_RESPONSE):
    response = None
    try:
        hdr = {'User-Agent': 'Hamle Internal Production Proof (ahmet@gamechangers.com.tr)'}
        if token is not None:
            hdr['Authorization'] = 'Bearer ' + provider._token(token)
        hdr.update(headers or {})
        response = session.request(method, url, headers=hdr, data=data, timeout=(10, 30), allow_redirects=False, stream=True)
        require(response.status_code == expected, 'provider_response_unconfirmed')
        require('rel="next"' not in response.headers.get('Link', ''), 'provider_pagination_unhandled')
        chunks, length = [], 0
        for chunk in response.iter_content(chunk_size=16384):
            length += len(chunk)
            require(length <= limit, 'provider_response_too_large')
            chunks.append(chunk)
        body = b''.join(chunks)
        if raw:
            return body
        value = provider._json(body)
        require(isinstance(value, dict), 'provider_response_invalid')
        return value
    except InternalProviderError:
        raise
    except Exception:
        raise InternalProviderError('provider_outcome_unknown_no_retry') from None
    finally:
        if response is not None:
            try: response.close()
            except Exception: pass


def _task_payload(title, body):
    return {'account_id': ACCOUNT, 'project_id': PROJECT, 'todo_parent_id': PARENT, 'todo_parent_type': 'Todoset',
            'title': title, 'description': body, 'assignee_id': HAMLE_ID, 'due_at': None}


class _WithoutAttachments(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=False)
        self.output = []; self.sgids = []; self.depth = 0; self.bad = False
    def handle_starttag(self, tag, attrs):
        if tag == 'bc-attachment':
            self.depth += 1
            vals = [v for k, v in attrs if k == 'sgid']
            if len(vals) != 1: self.bad = True
            else: self.sgids.append(vals[0])
        elif self.depth == 0:
            # Basecamp normalizes either paragraph wrapper with dir=auto.
            self.output.append('<p>' if tag == 'p' and attrs == [('dir', 'auto')] else self.get_starttag_text())
    def handle_endtag(self, tag):
        if tag == 'bc-attachment':
            self.depth -= 1
            if self.depth < 0: self.bad = True
        elif self.depth == 0: self.output.append('</' + tag + '>')
    def handle_data(self, data):
        if self.depth == 0: self.output.append(data)
    def handle_entityref(self, name):
        if self.depth == 0: self.output.append('&' + name + ';')
    def handle_charref(self, name):
        if self.depth == 0: self.output.append('&#' + name + ';')


class InternalProviders:
    def __init__(self, secrets, claim_refresh=None, allowed_task_ids=()):
        self.secrets, self.claim_refresh = secrets, claim_refresh
        self.allowed_task_ids = frozenset(_id(x) for x in allowed_task_ids)
        require(not self.allowed_task_ids.intersection(OLD_TASKS), 'historical_task_forbidden')
        self.writer = writers.BusinessWriters(secrets, claim_refresh)

    def capture_source(self):
        return provider.capture_internal_request(provider.GmailSecretStore(self.secrets),
            allow_body_for_internal_request=True, exact_subject=SUBJECT)

    def verify_internal(self):
        token = basecamp_cloud_auth.access_token(self.secrets, claim_refresh=self.claim_refresh)
        value = provider.verify_basecamp_internal(token)
        require(value.get('todo_parent_id') == PARENT and value.get('todo_parent_type') == 'Todoset', 'internal_parent_changed')
        return value

    def _basecamp(self):
        return self.writer._basecamp(_task_payload('internal', 'internal'))

    def _bound_task(self, task_id):
        tid = _id(task_id)
        require(tid not in OLD_TASKS, 'historical_task_forbidden')
        require(tid in self.allowed_task_ids, 'task_not_durably_bound')
        return tid

    def _task_current(self, session, token, task_id):
        tid = self._bound_task(task_id)
        row = _transport(session, 'GET', f'{PROJECT_BASE}/todos/{tid}.json', token)
        require(row.get('id') == tid and row.get('bucket', {}).get('id') == PROJECT
            and row.get('parent', {}).get('id') == PARENT and row.get('parent', {}).get('type') == 'Todoset'
            and row.get('creator', {}).get('id') == HAMLE_ID and row.get('status') == 'active'
            and row.get('visible_to_clients') is False and row.get('completed') is False
            and [x.get('id') for x in row.get('assignees', [])] == [HAMLE_ID], 'internal_task_scope_changed')
        return tid

    def create_task(self, title, body, *, before_post=None):
        require(callable(before_post), 'durable_before_post_required')
        result = self.writer.create_task(_task_payload(title, body), before_post=before_post)
        require(_id(result.get('provider_id')) not in OLD_TASKS, 'historical_task_forbidden')
        return result

    def read_task(self, provider_id, title, body):
        require(_id(provider_id) not in OLD_TASKS, 'historical_task_forbidden')
        return self.writer.read_task(str(provider_id), _task_payload(title, body))

    def upload_attachment(self, name, data, mime, *, before_post=None):
        require(callable(before_post), 'durable_before_post_required')
        metadata = _file(name, data, mime)
        session = provider._session()
        try:
            token = self._basecamp()
            before_post()
            row = _transport(session, 'POST', BC + '/attachments.json?name=' + quote(name, safe=''), token,
                             data=data, expected=201, headers={'Content-Type': mime, 'Content-Length': str(len(data))})
            result = {**_attachment_identifier(row), 'attachable_sgid': row.get('attachable_sgid'), **metadata}
            _receipt(result)
            require(all(k not in row or row[k] == metadata[k] for k in ('filename', 'content_type', 'byte_size')), 'attachment_upload_metadata_mismatch')
            return result
        finally:
            session.close()

    def create_comment(self, task_id, body, *, before_post=None, attachments=()):
        require(callable(before_post), 'durable_before_post_required')
        tid = self._bound_task(task_id)
        writers._text(body, 4000)
        attachments = [_receipt(x) for x in attachments]
        require(len(attachments) <= 2 and len({x['provider_id'] for x in attachments}) == len(attachments), 'attachment_receipt_invalid')
        content = writers._description(body)
        content += ''.join('<bc-attachment sgid="' + escape(x['attachable_sgid'], quote=True) + '"></bc-attachment>' for x in attachments)
        session = provider._session()
        try:
            token = self._basecamp()
            self._task_current(session, token, tid)
            before_post()
            row = _transport(session, 'POST', f'{PROJECT_BASE}/recordings/{tid}/comments.json', token,
                data=provider._encode({'content': content}), expected=201, headers={'Content-Type': 'application/json'})
            require(type(row.get('id')) is int and row['id'] > 0 and row.get('bucket', {}).get('id') == PROJECT
                    and row.get('parent', {}).get('id') == tid and row.get('parent', {}).get('type') == 'Todo', 'comment_receipt_unconfirmed')
            return {'provider_id': str(row['id'])}
        finally:
            session.close()

    def _comment(self, session, token, comment_id, task_id):
        cid = _id(comment_id)
        self._task_current(session, token, task_id)
        row = _transport(session, 'GET', f'{PROJECT_BASE}/comments/{cid}.json', token)
        require(row.get('id') == cid and row.get('type') == 'Comment' and row.get('bucket', {}).get('id') == PROJECT
                and row.get('parent', {}).get('id') == int(task_id) and row.get('parent', {}).get('type') == 'Todo'
                and row.get('creator', {}).get('id') == HAMLE_ID and row.get('status') == 'active'
                and row.get('visible_to_clients') is False, 'comment_scope_mismatch')
        return row

    @staticmethod
    def _attachment_matches(row, expected):
        identifier = _attachment_identifier(row)
        id_matches = (expected.get('identifier_kind') == 'sgid_digest' or identifier['provider_id'] == expected['provider_id'])
        return (id_matches and row.get('sgid', row.get('attachable_sgid')) == expected['attachable_sgid']
                and all(row.get(k) == expected[k] for k in ('filename', 'content_type', 'byte_size')))

    def read_comment(self, provider_id, task_id, body, attachments=()):
        self._bound_task(task_id); writers._text(body, 4000)
        attachments = [_receipt(x) for x in attachments]
        session = provider._session()
        try:
            row = self._comment(session, self._basecamp(), provider_id, task_id)
            actual = row.get('content_attachments')
            require(isinstance(actual, list) and len(actual) == len(attachments), 'comment_attachments_mismatch')
            require(all(self._attachment_matches(a, e) for a, e in zip(actual, attachments)), 'comment_attachments_mismatch')
            parser = _WithoutAttachments(); parser.feed(row.get('content', '')); parser.close()
            require(not parser.bad and parser.depth == 0 and parser.sgids == [x['attachable_sgid'] for x in attachments]
                    and writers._description_matches(''.join(parser.output), body), 'comment_body_mismatch')
            return {'provider_id': str(provider_id), 'identity_verified': True, 'matches_approved_payload': True,
                    'attachment_count': len(attachments),
                    'attachment_receipts': [{**expected, **_attachment_identifier(actual)} for expected, actual in zip(attachments, actual)]}
        finally:
            session.close()

    def download_attachment(self, task_id, comment_id, attachment_receipt):
        self._bound_task(task_id); expected = _receipt(attachment_receipt)
        session = provider._session()
        try:
            token = self._basecamp()
            row = self._comment(session, token, comment_id, task_id)
            matches = [x for x in row.get('content_attachments', []) if self._attachment_matches(x, expected)]
            require(len(matches) == 1, 'attachment_download_binding_mismatch')
            url = matches[0].get('download_url'); require(isinstance(url, str), 'attachment_download_url_invalid')
            parsed = urlsplit(url)
            require(parsed.scheme == 'https' and parsed.netloc in ('3.basecampapi.com', 'storage.3.basecamp.com', 'storage.app.basecamp.com')
                    and parsed.path.startswith(f'/{ACCOUNT}/blobs/') and '/download/' in parsed.path
                    and not parsed.fragment, 'attachment_download_url_invalid')
            data = _download_bytes(session, url, token)
            require(len(data) == expected['byte_size'] and hashlib.sha256(data).hexdigest() == expected['sha256'], 'attachment_bytes_mismatch')
            _file(expected['filename'], data, expected['content_type'])
            return data
        finally:
            session.close()

    @staticmethod
    def _delivery(evidence, body, attachments):
        require(isinstance(evidence, dict) and evidence.get('subject') == SUBJECT and evidence.get('sender') == provider.AHMET
                and evidence.get('hamle_direct_recipient') is True and evidence.get('action_eligible') is False,
                'delivery_source_invalid')
        for field in ('thread_id', 'message_id'):
            require(isinstance(evidence.get(field), str) and re.fullmatch(r'[a-f0-9]{1,64}', evidence[field]), 'delivery_source_invalid')
        require(evidence['thread_id'] not in provider.FORBIDDEN_THREADS and evidence['thread_id'] != provider.CONTINUATION_THREAD,
                'historical_thread_forbidden')
        recipients = derive_reply_recipients(evidence.get('latest_to'), evidence.get('latest_cc'))
        writers._text(body, 4000)
        for field in ('in_reply_to', 'references'):
            writers._text(evidence.get(field), 4096, header=True)
        require(re.fullmatch(writers.MESSAGE_ID, evidence['in_reply_to']) and
                all(re.fullmatch(writers.MESSAGE_ID, x) for x in evidence['references'].split())
                and evidence['in_reply_to'] in evidence['references'].split(), 'delivery_rfc_headers_invalid')
        require(isinstance(attachments, (list, tuple)) and len(attachments) == 2, 'two_delivery_pngs_required')
        files = [_file(x.get('filename'), x.get('data'), x.get('content_type')) for x in attachments]
        require(len({x['filename'] for x in files}) == 2, 'delivery_filename_duplicate')
        stable = {'thread_id': evidence['thread_id'], 'in_reply_to': evidence['in_reply_to'],
                  'references': evidence['references'], 'subject': 'Re: ' + SUBJECT, 'body': body, **recipients, 'files': files}
        message_id = '<hamle-production-' + hashlib.sha256(provider._encode(stable)).hexdigest() + '@gamechangers.com.tr>'
        message = EmailMessage(policy=policy.SMTP)
        message['From'] = provider.HAMLE; message['To'] = provider.AHMET
        if recipients['cc']: message['Cc'] = ', '.join(recipients['cc'])
        message['Subject'] = stable['subject']; message['In-Reply-To'] = evidence['in_reply_to']
        message['References'] = evidence['references']; message['Message-ID'] = message_id
        message.set_content(body, subtype='plain', charset='utf-8')
        for file in attachments:
            message.add_attachment(file['data'], maintype='image', subtype='png', filename=file['filename'])
        return message.as_bytes(), stable, message_id

    def send_delivery(self, evidence, body, attachments, *, before_post=None):
        require(callable(before_post), 'durable_before_post_required')
        raw, _, _ = self._delivery(evidence, body, attachments)
        with self.writer._gmail() as token:
            session = provider._session()
            try:
                before_post()
                row = _transport(session, 'POST', writers.GMAIL_BASE + '/messages/send', token,
                    data=provider._encode({'threadId': evidence['thread_id'], 'raw': base64.urlsafe_b64encode(raw).decode()}),
                    headers={'Content-Type': 'application/json'})
                require(isinstance(row.get('id'), str) and re.fullmatch(r'[a-f0-9]{1,64}', row['id'])
                        and row.get('threadId') == evidence['thread_id'], 'delivery_receipt_unconfirmed')
                return {'provider_id': row['id']}
            finally:
                session.close()

    def read_delivery(self, provider_id, evidence, body, attachments):
        _, stable, message_id = self._delivery(evidence, body, attachments)
        require(isinstance(provider_id, str) and re.fullmatch(r'[a-f0-9]{1,64}', provider_id), 'delivery_receipt_invalid')
        with self.writer._gmail() as token:
            session = provider._session()
            try:
                row = _transport(session, 'GET', writers.GMAIL_BASE + f'/messages/{provider_id}?format=raw&fields=id%2CthreadId%2ClabelIds%2Craw', token)
                require(row.get('id') == provider_id and row.get('threadId') == evidence['thread_id'] and 'SENT' in row.get('labelIds', []), 'delivery_readback_receipt_mismatch')
                raw = row.get('raw')
                require(isinstance(raw, str) and re.fullmatch(r'[A-Za-z0-9_-]+={0,2}', raw), 'delivery_readback_invalid')
                message = BytesParser(policy=policy.default).parsebytes(base64.b64decode(raw + '=' * (-len(raw) % 4), altchars=b'-_', validate=True))
                def one(name):
                    fields = message.get_all(name, [])
                    require(len(fields) == 1 and not getattr(fields[0], 'defects', ()), 'delivery_readback_header_mismatch')
                    return str(fields[0])
                returned_id = one('Message-ID')
                require(getaddresses([one('From')]) == [('', provider.HAMLE)] and getaddresses([one('To')]) == [('', provider.AHMET)]
                        and ((not message.get_all('Cc')) if not stable['cc'] else [a for _, a in getaddresses([one('Cc')])] == stable['cc'])
                        and not message.get_all('Bcc') and one('Subject') == stable['subject']
                        and one('In-Reply-To') == stable['in_reply_to'] and ' '.join(one('References').split()) == stable['references']
                        and (returned_id == message_id or (re.fullmatch(writers.MESSAGE_ID, returned_id) and returned_id.endswith('@mail.gmail.com>')))
                        and message.get_content_type() == 'multipart/mixed' and not message.defects, 'delivery_readback_header_mismatch')
                parts = list(message.iter_parts())
                require(len(parts) == 3 and parts[0].get_content_type() == 'text/plain' and not parts[0].is_multipart(), 'delivery_readback_body_mismatch')
                require(parts[0].get_content().replace('\r\n', '\n') == body + ('' if body.endswith('\n') else '\n'), 'delivery_readback_body_mismatch')
                for part, expected in zip(parts[1:], stable['files']):
                    data = part.get_payload(decode=True)
                    require(not part.is_multipart() and not part.defects and part.get_content_disposition() == 'attachment'
                            and part.get_filename() == expected['filename'] and part.get_content_type() == expected['content_type']
                            and type(data) is bytes and len(data) == expected['byte_size'] and hashlib.sha256(data).hexdigest() == expected['sha256'], 'delivery_readback_attachment_mismatch')
                return {'provider_id': provider_id, 'identity_verified': True, 'matches_approved_payload': True,
                        'thread_id': evidence['thread_id'], 'attachment_sha256': [x['sha256'] for x in stable['files']]}
            except InternalProviderError:
                raise
            except Exception:
                raise InternalProviderError('delivery_readback_unconfirmed') from None
            finally:
                session.close()
