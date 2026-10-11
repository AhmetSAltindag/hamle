"""Bounded read-only Basecamp observations; not a delivery or authority adapter.

Official routes and Link/ETag semantics checked 2026-10-10:
https://github.com/basecamp/bc-api/blob/master/sections/todos.md
https://github.com/basecamp/bc-api/blob/master/sections/comments.md
https://github.com/basecamp/bc-api/blob/master/README.md#pagination

No credential discovery, network request or V7 change occurs on import. Caller
supplies a GET transport and an already authorized work binding. Text, files and
"ready" comments remain untrusted observations, never producer release or QA.
"""
from __future__ import annotations

import copy
import hashlib
import json
import re
from datetime import datetime, timezone
from urllib.parse import urlsplit, parse_qs


class ReadError(ValueError):
    pass


def need(ok, code):
    if not ok:
        raise ReadError(code)


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
                                    separators=(',', ':'), allow_nan=False).encode()).hexdigest()


class SessionGet:
    """Explicit session/token callback; GET only, no redirects or hidden retries.

    Use cloud_providers._session() for a zero-retry transport. This helper does
    not refresh tokens itself; renewal and secure storage belong to the caller.
    Error messages intentionally contain no response body, URL or credential.
    """
    def __init__(self, session, token_callback):
        self.session, self.token_callback = session, token_callback

    def __call__(self, url, conditional_headers):
        from cloud_providers import _token, _json
        response = None
        try:
            p = urlsplit(url)
            need(p.scheme == 'https' and p.netloc == '3.basecampapi.com' and not p.fragment
                 and re.fullmatch(r'/[1-9][0-9]*/(?:buckets/[1-9][0-9]*/)?(?:todos/[1-9][0-9]*|recordings/[1-9][0-9]*/comments)\.json', p.path),
                 'transport_url_out_of_scope')
            query = parse_qs(p.query, strict_parsing=True, keep_blank_values=True)
            need(not query or (set(query) == {'page'} and len(query['page']) == 1
                 and re.fullmatch(r'[1-9][0-9]{0,5}', query['page'][0])), 'transport_query_invalid')
            need(isinstance(conditional_headers, dict)
                 and set(conditional_headers) <= {'If-None-Match', 'If-Modified-Since'}
                 and all(isinstance(v, str) and len(v) <= 1024 and '\r' not in v and '\n' not in v
                         for v in conditional_headers.values()), 'transport_headers_invalid')
            headers = {'Authorization': 'Bearer ' + _token(self.token_callback()),
                       'User-Agent': 'Hamle Work Tracking (ahmet@gamechangers.com.tr)',
                       'Accept': 'application/json', **conditional_headers}
            response = self.session.request('GET', url, headers=headers,
                timeout=(5, 15), allow_redirects=False, stream=True)
            need(response.status_code in (200, 304),
                 'basecamp_rate_limited' if response.status_code == 429 else 'basecamp_read_rejected')
            if response.status_code == 304:
                return 304, None, dict(response.headers)
            chunks, size = [], 0
            for chunk in response.iter_content(chunk_size=16384):
                size += len(chunk)
                need(size <= 1_048_576, 'basecamp_page_too_large')
                chunks.append(chunk)
            return 200, _json(b''.join(chunks)), dict(response.headers)
        except ReadError:
            raise
        except Exception:
            raise ReadError('basecamp_read_failed') from None
        finally:
            if response is not None:
                try:
                    response.close()
                except Exception:
                    pass


class BasecampReader:
    def __init__(self, get, *, max_pages=12, max_comments=1000, max_cache=64):
        need(type(max_pages) is int and 1 <= max_pages <= 20, 'page_limit_invalid')
        need(type(max_comments) is int and 1 <= max_comments <= 2000, 'comment_limit_invalid')
        need(type(max_cache) is int and 1 <= max_cache <= 256, 'cache_limit_invalid')
        self.get, self.max_pages, self.max_comments, self.max_cache = get, max_pages, max_comments, max_cache
        self.cache = {}

    @staticmethod
    def _binding(work):
        need(isinstance(work, dict), 'work_missing')
        binding = {}
        for k in ('account_id', 'project_id', 'list_id', 'task_id'):
            need(type(work.get(k)) is int and work[k] > 0, 'work_binding_invalid')
            binding[k] = work[k]
        need(isinstance(work.get('work_id'), str) and 0 < len(work['work_id']) <= 200,
             'work_binding_invalid')
        binding['work_id'] = work['work_id']
        return binding

    @staticmethod
    def _check_url(url, paths):
        need(isinstance(url, str) and len(url) <= 2048, 'basecamp_url_invalid')
        p = urlsplit(url)
        need(p.scheme == 'https' and p.netloc == '3.basecampapi.com'
             and p.path in paths and not p.fragment, 'basecamp_url_out_of_scope')
        try:
            query = parse_qs(p.query, strict_parsing=True, keep_blank_values=True)
        except ValueError:
            raise ReadError('basecamp_page_query_invalid') from None
        need(not query or (set(query) == {'page'} and len(query['page']) == 1
                           and re.fullmatch(r'[1-9][0-9]{0,5}', query['page'][0])),
             'basecamp_page_query_invalid')
        return url

    @staticmethod
    def _next(headers):
        link = headers.get('link', '')
        need(isinstance(link, str) and len(link) <= 4096, 'basecamp_link_invalid')
        if not link.strip():
            return None
        nexts = []
        for part in link.split(','):
            match = re.fullmatch(r'\s*<([^<>]+)>\s*;\s*rel="([a-z ]+)"\s*', part)
            need(match is not None, 'basecamp_link_invalid')
            if 'next' in match[2].split():
                nexts.append(match[1])
        need(len(nexts) <= 1, 'basecamp_link_ambiguous')
        return nexts[0] if nexts else None

    def _read(self, url, paths):
        self._check_url(url, paths)
        cached = self.cache.get(url)
        conditional = {}
        if cached:
            if cached[1].get('etag'):
                conditional['If-None-Match'] = cached[1]['etag']
            elif cached[1].get('last-modified'):
                conditional['If-Modified-Since'] = cached[1]['last-modified']
        try:
            status, payload, headers = self.get(url, conditional)
        except Exception:
            raise ReadError('basecamp_transport_failed') from None
        need(type(status) is int and status in (200, 304) and isinstance(headers, dict),
             'basecamp_response_invalid')
        headers = {k.lower(): v for k, v in headers.items()}
        if status == 304:
            need(cached is not None and bool(conditional), 'basecamp_304_without_cache')
            payload, old_headers = cached
            headers = {**old_headers, **headers}
        need(len(json.dumps(payload, allow_nan=False).encode()) <= 1_048_576, 'basecamp_page_too_large')
        self.cache[url] = copy.deepcopy((payload, headers))
        if len(self.cache) > self.max_cache:
            self.cache.pop(next(iter(self.cache)))
        return copy.deepcopy(payload), headers

    @staticmethod
    def _record(record, *, record_type, project, parent, expected_id=None):
        need(isinstance(record, dict) and record.get('type') == record_type
             and type(record.get('id')) is int and record['id'] > 0
             and (expected_id is None or record['id'] == expected_id)
             and isinstance(record.get('bucket'), dict) and isinstance(record.get('parent'), dict)
             and type(record['bucket'].get('id')) is int and type(record['parent'].get('id')) is int
             and record.get('bucket', {}).get('id') == project
             and record.get('parent', {}).get('id') == parent,
             'basecamp_record_binding_mismatch')
        need(record.get('status') == 'active' and type(record.get('visible_to_clients')) is bool,
             'basecamp_record_state_invalid')

    def poll(self, work, *, previous=None, observed_at=None):
        """Return a complete bounded observation, or fail without a new cursor.

        Every poll walks the current active-comment list, using conditional GETs
        to also detect edits/removals. A partial page traversal is never treated
        as the whole task. Provider pagination is not an atomic snapshot; final
        delivery must independently re-read all current facts before dispatch.
        """
        b = self._binding(work)
        prior = previous if previous is not None else {'binding': b, 'task_sha256': None, 'comments': {}, 'sequence': 0}
        need(isinstance(prior, dict) and prior.get('binding') == b and isinstance(prior.get('comments'), dict)
             and len(prior['comments']) <= self.max_comments
             and type(prior.get('sequence')) is int and prior['sequence'] >= 0
             and 'task_sha256' in prior and (prior['task_sha256'] is None or
                 isinstance(prior['task_sha256'], str) and re.fullmatch(r'[0-9a-f]{64}', prior['task_sha256']))
             and all(isinstance(k, str) and re.fullmatch(r'[1-9][0-9]{0,29}', k)
                     and isinstance(v, str) and re.fullmatch(r'[0-9a-f]{64}', v)
                     for k,v in prior['comments'].items()), 'checkpoint_binding_mismatch')
        root = f"https://3.basecampapi.com/{b['account_id']}"
        task_paths = {f"/{b['account_id']}/todos/{b['task_id']}.json",
                      f"/{b['account_id']}/buckets/{b['project_id']}/todos/{b['task_id']}.json"}
        comment_paths = {f"/{b['account_id']}/recordings/{b['task_id']}/comments.json",
                         f"/{b['account_id']}/buckets/{b['project_id']}/recordings/{b['task_id']}/comments.json"}
        task_url = root + f"/todos/{b['task_id']}.json"
        task, _ = self._read(task_url, task_paths)
        self._record(task, record_type='Todo', project=b['project_id'], parent=b['list_id'], expected_id=b['task_id'])
        need(type(task.get('completed')) is bool and isinstance(task.get('assignees'), list), 'basecamp_task_invalid')
        need(type(task.get('comments_count')) is int and 0 <= task['comments_count'] <= self.max_comments,
             'basecamp_comment_limit')
        comments_url = self._check_url(task.get('comments_url'), comment_paths)
        current, pages, visited = {}, 0, set()
        while comments_url:
            need(pages < self.max_pages, 'basecamp_page_limit')
            need(comments_url not in visited, 'basecamp_pagination_cycle')
            visited.add(comments_url)
            rows, headers = self._read(comments_url, comment_paths)
            pages += 1
            need(isinstance(rows, list) and len(rows) <= 100, 'basecamp_comment_page_invalid')
            for comment in rows:
                self._record(comment, record_type='Comment', project=b['project_id'], parent=b['task_id'])
                need(str(comment['id']) not in current, 'basecamp_duplicate_comment')
                need(len(current) < self.max_comments, 'basecamp_comment_limit')
                current[str(comment['id'])] = comment
            comments_url = self._next(headers)
        end_task, _ = self._read(task_url, task_paths)
        need(digest(task) == digest(end_task) and len(current) == task['comments_count'],
             'basecamp_changed_during_poll')
        now = observed_at or datetime.now(timezone.utc)
        need(isinstance(now, datetime) and now.tzinfo is not None, 'observation_time_invalid')
        changes, hashes = [], {key: digest(value) for key, value in current.items()}
        if prior['task_sha256'] != digest(task):
            changes.append({'kind': 'task_observed', 'record_id': b['task_id'], 'record_sha256': digest(task)})
        for key in sorted(set(hashes) | set(prior['comments']), key=int):
            if hashes.get(key) != prior['comments'].get(key):
                changes.append({'kind': 'comment_observed' if key in hashes else 'comment_absent',
                                'record_id': int(key), 'record_sha256': hashes.get(key)})
        sequence = prior['sequence']
        for change in changes:
            sequence += 1
            change.update(source_order=sequence, event_id='bc-' + digest([b, sequence, change]),
                          untrusted_content=True, grants_authority=False)
        checkpoint = {'binding': b, 'task_sha256': digest(task), 'comments': hashes, 'sequence': sequence}
        return {'binding': b, 'observed_at': now.isoformat(), 'task': task, 'comments': list(current.values()),
                'changes': changes, 'checkpoint': checkpoint, 'fingerprint': digest(checkpoint),
                'pages_read': pages, 'complete': True, 'has_more': False, 'live_enabled': False,
                'artifact_bytes_verified': False, 'producer_release_verified': False,
                'quality_accepted': False, 'delivery_authorized': False}
