"""Narrow cloud reads of explicitly user-selected historical WAT sources.

No business write method exists. OAuth refresh uses only the existing fenced
cloud pathway, and requires its permanent claim callback if a token expires.
"""
from __future__ import annotations
import hashlib
import json
import re
from urllib.parse import unquote, urlsplit

import basecamp_cloud_auth
import cloud_providers as provider

ACCOUNT = 5898865
PROJECT = 40694338
TASKS = frozenset((10309337809, 10064527849, 10317695863))
PRODUCERS = frozenset((47658175, 50486696))  # Berkay and Samet
SAMET = 50486696
GUIDE_UPLOAD = 10392828700
GUIDE_SIZE = 9910859
GUIDE_SHA256 = '6be9fc432313ed5fd6d7741d52e83e584dddd26b8802238a01aee8b05ca3881c'
BASE = f'https://3.basecampapi.com/{ACCOUNT}/buckets/{PROJECT}'
PROFILE = f'https://3.basecampapi.com/{ACCOUNT}/my/profile.json'
MAX_IMAGE = 16 * 1024 * 1024
MAX_VIDEO = 64 * 1024 * 1024
MAX_METADATA = 4 * 1024 * 1024


class WATReadError(Exception):
    retry_allowed = False
    def __init__(self, code):
        super().__init__(code)
        self.code = code


def require(ok, code):
    if not ok:
        raise WATReadError(code)


def identifier(value):
    require(type(value) is int and 0 < value < 10**30 or
            isinstance(value, str) and re.fullmatch('[1-9][0-9]{0,29}', value), 'wat_identifier_invalid')
    return int(value)


def _expectation(sha, size, limit):
    require(isinstance(sha, str) and re.fullmatch('[0-9a-f]{64}', sha), 'wat_expected_hash_required')
    require(type(size) is int and 0 < size <= limit, 'wat_expected_size_invalid')


def _safe_download(url, *, guide=False):
    require(isinstance(url, str) and len(url) <= 16384 and not any(ord(c) < 33 for c in url), 'wat_download_url_invalid')
    parsed = urlsplit(url)
    decoded = unquote(parsed.path)
    allowed_path = decoded.startswith(f'/{ACCOUNT}/blobs/') and '/download/' in decoded
    if guide:
        allowed_path = allowed_path or decoded.startswith(f'/{ACCOUNT}/buckets/{PROJECT}/uploads/{GUIDE_UPLOAD}/download/')
    require(parsed.scheme == 'https' and parsed.netloc in ('3.basecampapi.com', 'storage.3.basecamp.com', 'storage.app.basecamp.com')
            and allowed_path and not parsed.fragment and '\\' not in decoded
            and not any(x in ('.', '..') for x in decoded.split('/')), 'wat_download_url_invalid')
    return url


class WATReadOnly:
    def __init__(self, manager, claim_refresh=None):
        self.manager = manager
        self.claim_refresh = claim_refresh
        self._session = None
        self._closed = False

    def close(self):
        if self._session is not None:
            self._session.close()
            self._session = None
        self._closed = True

    def _get_session(self):
        require(not self._closed, 'wat_reader_closed')
        if self._session is None:
            self._session = provider._session()
        return self._session

    def _read(self, url, access=None, *, expected_size=None, permit_redirect=False):
        """GET only; all URLs originate in exact bindings below, never user input."""
        session = self._get_session()
        response = None
        try:
            headers = {'User-Agent': 'Hamle authorized WAT read-only shadow'}
            if access is not None:
                headers['Authorization'] = 'Bearer ' + provider._token(access)
            response = session.request('GET', url, headers=headers, timeout=(10, 45), allow_redirects=False, stream=True)
            if response.status_code == 302 and permit_redirect:
                target = response.headers.get('Location', '')
                parsed = urlsplit(target)
                require(isinstance(target, str) and len(target) <= 16384 and not any(ord(c) < 33 for c in target)
                        and parsed.scheme == 'https' and parsed.netloc == 'storage.basecamp.com'
                        and not parsed.fragment, 'wat_storage_redirect_invalid')
                response.close()
                response = None
                response = session.request('GET', target, headers={'User-Agent': headers['User-Agent']},
                    timeout=(10, 45), allow_redirects=False, stream=True)
            require(response.status_code == 200, 'wat_read_failed_no_retry')
            require('rel="next"' not in response.headers.get('Link', ''), 'wat_pagination_unhandled')
            chunks, count = [], 0
            limit = expected_size if expected_size is not None else MAX_METADATA
            for chunk in response.iter_content(chunk_size=16384):
                count += len(chunk)
                require(count <= limit, 'wat_response_too_large')
                chunks.append(chunk)
            data = b''.join(chunks)
            if expected_size is not None:
                require(len(data) == expected_size, 'wat_attachment_size_changed')
                return data
            row = json.loads(data)
            require(isinstance(row, dict), 'wat_metadata_invalid')
            return row
        except WATReadError:
            raise
        except Exception:
            raise WATReadError('wat_read_failed_no_retry') from None
        finally:
            if response is not None:
                try: response.close()
                except Exception: pass

    def _access(self):
        self._get_session()
        try:
            access = basecamp_cloud_auth.access_token(self.manager, claim_refresh=self.claim_refresh)
        except basecamp_cloud_auth.AuthError as error:
            raise WATReadError(error.code) from None
        except Exception:
            raise WATReadError('wat_auth_unavailable') from None
        row = self._read(PROFILE, access)
        require(row.get('id') == 53358100 and row.get('email_address') == 'hamle@gamechangers.com.tr', 'wat_identity_mismatch')
        return access

    def _bindings(self, task_id, comment_id, producer_id):
        tid, cid, pid = identifier(task_id), identifier(comment_id), identifier(producer_id)
        require(tid in TASKS, 'wat_task_not_authorized')
        require(pid in PRODUCERS, 'wat_producer_not_authorized')
        return tid, cid, pid

    def _comment(self, tid, cid, pid, access):
        task = self._read(f'{BASE}/todos/{tid}.json', access)
        require(task.get('id') == tid and task.get('type') == 'Todo' and task.get('bucket', {}).get('id') == PROJECT
                and task.get('status') == 'active' and task.get('visible_to_clients') is False, 'wat_task_binding_changed')
        row = self._read(f'{BASE}/comments/{cid}.json', access)
        require(row.get('id') == cid and row.get('type') == 'Comment' and row.get('bucket', {}).get('id') == PROJECT
                and row.get('parent', {}).get('id') == tid and row.get('parent', {}).get('type') == 'Todo'
                and row.get('creator', {}).get('id') == pid and row.get('status') == 'active'
                and row.get('visible_to_clients') is False, 'wat_comment_binding_changed')
        attachments = row.get('content_attachments')
        require(isinstance(attachments, list) and len(attachments) <= 100
                and all(isinstance(x, dict) for x in attachments), 'wat_attachment_manifest_invalid')
        ids = [identifier(x.get('id')) for x in attachments]
        require(len(ids) == len(set(ids)), 'wat_attachment_manifest_ambiguous')
        return row

    def manifest(self, task_id, comment_id, producer_id):
        tid, cid, pid = self._bindings(task_id, comment_id, producer_id)
        access = self._access()
        row = self._comment(tid, cid, pid, access)
        return {'account_id':ACCOUNT, 'project_id':PROJECT, 'task_id':tid, 'comment_id':cid,
            'producer_id':pid, 'comment_created_at':row.get('created_at'), 'comment_updated_at':row.get('updated_at'),
            'attachments':[{k:x.get(k) for k in ('id', 'filename', 'content_type', 'byte_size', 'width', 'height')}
                for x in row['content_attachments']], 'business_writes':0, 'action_eligible':False}

    def download(self, task_id, comment_id, attachment_id, expected_sha256, expected_size, producer_id):
        tid, cid, pid = self._bindings(task_id, comment_id, producer_id)
        aid = identifier(attachment_id)
        _expectation(expected_sha256, expected_size, MAX_VIDEO if pid == SAMET else MAX_IMAGE)
        access = self._access()
        row = self._comment(tid, cid, pid, access)
        selected = [x for x in row['content_attachments'] if x.get('id') == aid]
        require(len(selected) == 1, 'wat_attachment_not_in_comment')
        entry = selected[0]
        mime = entry.get('content_type')
        image = mime in ('image/png', 'image/jpeg')
        video = mime == 'video/mp4' and pid == SAMET and tid == 10317695863
        require(image or video, 'wat_attachment_type_not_allowed')
        require(expected_size <= (MAX_IMAGE if image else MAX_VIDEO), 'wat_expected_size_invalid')
        require(type(entry.get('byte_size')) is int and entry['byte_size'] == expected_size, 'wat_attachment_size_changed')
        url = _safe_download(entry.get('download_url'))
        data = self._read(url, access, expected_size=expected_size, permit_redirect=True)
        require(hashlib.sha256(data).hexdigest() == expected_sha256, 'wat_attachment_hash_changed')
        if mime == 'image/png':
            require(data.startswith(b'\x89PNG\r\n\x1a\n'), 'wat_attachment_signature_invalid')
        elif mime == 'image/jpeg':
            require(data.startswith(b'\xff\xd8\xff') and data.endswith(b'\xff\xd9'), 'wat_attachment_signature_invalid')
        else:
            require(len(data) >= 16 and data[4:8] == b'ftyp', 'wat_attachment_signature_invalid')
        return data

    def guide_metadata(self):
        access = self._access()
        row = self._guide(access)
        return {k:row.get(k) for k in ('id', 'filename', 'content_type', 'byte_size', 'created_at', 'updated_at')}

    def _guide(self, access):
        row = self._read(f'{BASE}/uploads/{GUIDE_UPLOAD}.json', access)
        require(row.get('id') == GUIDE_UPLOAD and row.get('bucket', {}).get('id') == PROJECT
            and row.get('creator', {}).get('id') == 47470759 and row.get('status') == 'active'
            and row.get('visible_to_clients') is False and row.get('content_type') == 'application/pdf'
            and row.get('byte_size') == GUIDE_SIZE, 'wat_approved_guide_changed')
        return row

    def download_guide(self):
        """Only the exact PDF hash explicitly approved by the user; no other PDF."""
        access = self._access()
        row = self._guide(access)
        data = self._read(_safe_download(row.get('download_url'), guide=True), access,
            expected_size=GUIDE_SIZE, permit_redirect=True)
        require(data.startswith(b'%PDF-') and hashlib.sha256(data).hexdigest() == GUIDE_SHA256, 'wat_approved_guide_changed')
        return data
