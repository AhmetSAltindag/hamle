"""Read only the three user-selected WAT references; bounded native attachments.

OAuth stays in memory. Native download URLs/SGIDs are never saved or printed.
No business writes, model calls or invocation of downloaded source files.
"""
from datetime import datetime, timezone
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import sys
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent / 'hamle-account-manager'))
import cloud_providers
from read_wat_current_asset import token
from read_wat_example_read_only import content, person
from verify_wat_read_only import ROOT, read

ACCOUNT, PROJECT = 5898865, 40694338
TASKS = (10309337809, 10064527849, 10317695863)
BASE = f'https://3.basecampapi.com/{ACCOUNT}/buckets/{PROJECT}'
OUT = ROOT / 'outputs/wat-pilot-20261010/real-output-shadow'
MAX_FILE = 64 * 1024 * 1024
MAX_TOTAL = 160 * 1024 * 1024


def require(value, code):
    if not value:
        raise RuntimeError(code)


def save(path, value):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def safe_content(raw):
    parsed = content(raw)
    return {'text': parsed['text'], 'html_attachment_attributes': parsed['attachments'],
            'linked_hosts': sorted(set(x['host'] for x in parsed['link_metadata']))}


def get(session, url, access, *, expected_path=None):
    parsed = urlsplit(url)
    require(parsed.scheme == 'https' and parsed.netloc == '3.basecampapi.com'
            and parsed.path.startswith(f'/{ACCOUNT}/buckets/{PROJECT}/')
            and not parsed.fragment, 'native_read_scope_invalid')
    if expected_path is not None:
        require(parsed.path == expected_path, 'pagination_path_changed')
    response = session.request('GET', url, headers={'Authorization': 'Bearer ' + access,
        'User-Agent': 'Hamle read-only selected WAT references'}, timeout=(10, 45),
        allow_redirects=False, stream=True)
    try:
        require(response.status_code == 200, 'native_read_failed')
        parts, count = [], 0
        for chunk in response.iter_content(16384):
            count += len(chunk)
            require(count <= 8 * 1024 * 1024, 'native_response_too_large')
            parts.append(chunk)
        value = json.loads(b''.join(parts))
        next_links = re.findall(r'<([^>]+)>;\s*rel="next"', response.headers.get('Link', ''))
        require(len(next_links) <= 1, 'pagination_ambiguous')
        return value, next_links[0] if next_links else None
    finally:
        response.close()


def attachment(row):
    fields = ('id', 'filename', 'title', 'content_type', 'byte_size', 'width', 'height', 'created_at', 'updated_at')
    result = {k: row.get(k) for k in fields if k in row}
    result['download_url_present'] = bool(row.get('download_url') or row.get('url'))
    result['native_metadata_keys'] = sorted(row)
    return result


def download(session, row, access):
    expected = row.get('byte_size')
    require(type(expected) is int and 0 < expected <= MAX_FILE, 'download_size_not_bounded')
    url = row.get('download_url') or row.get('url')
    parsed = urlsplit(url or '')
    require(parsed.scheme == 'https' and parsed.netloc in ('3.basecampapi.com', 'storage.3.basecamp.com', 'storage.app.basecamp.com')
            and parsed.path.startswith(f'/{ACCOUNT}/blobs/') and '/download/' in parsed.path
            and not parsed.fragment, 'download_scope_invalid')
    response = session.request('GET', url, headers={'Authorization': 'Bearer ' + access},
        timeout=(10, 45), allow_redirects=False, stream=True)
    if response.status_code == 302:
        target = response.headers.get('Location', '')
        parsed = urlsplit(target)
        response.close()
        require(parsed.scheme == 'https' and parsed.netloc == 'storage.basecamp.com' and not parsed.fragment, 'download_redirect_invalid')
        response = session.request('GET', target, timeout=(10, 45), allow_redirects=False, stream=True)
    try:
        require(response.status_code == 200, 'download_failed')
        chunks, count = [], 0
        for chunk in response.iter_content(16384):
            count += len(chunk)
            require(count <= expected, 'download_size_changed')
            chunks.append(chunk)
        require(count == expected, 'download_size_changed')
        return b''.join(chunks)
    finally:
        response.close()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--download-selection')
    parser.add_argument('--download-manifest', default='selected-file-downloads.json')
    args = parser.parse_args()
    require(re.fullmatch('[a-z0-9-]{1,80}\\.json', args.download_manifest), 'manifest_name_invalid')
    os.umask(0o077)
    identity = read(['auth', 'status', '--check'])
    d = identity.get('data', {})
    require(identity.get('success') and d.get('valid') is True and d.get('storage') == 'keyring'
            and d.get('user_email') == 'hamle@gamechangers.com.tr'
            and str(d.get('account_id')) == str(ACCOUNT), 'identity_unverified')
    selections = json.loads(Path(args.download_selection).read_text()) if args.download_selection else []
    require(isinstance(selections, list) and len(selections) <= 16, 'selection_invalid')
    access = token()
    session = cloud_providers._session()
    summaries, downloaded, total = [], [], 0
    try:
        for task_id in TASKS:
            task, next_page = get(session, f'{BASE}/todos/{task_id}.json', access)
            require(not next_page and task.get('id') == task_id and task.get('bucket', {}).get('id') == PROJECT,
                    'task_binding_mismatch')
            comment_url = task.get('comments_url', '')
            expected_path = f'/{ACCOUNT}/buckets/{PROJECT}/recordings/{task_id}/comments.json'
            comments, seen_urls = [], set()
            while comment_url:
                require(comment_url not in seen_urls and len(seen_urls) < 10, 'pagination_unbounded')
                seen_urls.add(comment_url)
                page, comment_url = get(session, comment_url, access, expected_path=expected_path)
                require(isinstance(page, list), 'comments_invalid')
                comments.extend(page)
            require(len(comments) == task.get('comments_count'), 'comments_incomplete')
            require(len({c['id'] for c in comments}) == len(comments), 'duplicate_comments')
            evidence = {k: task.get(k) for k in ('id', 'title', 'status', 'completed', 'created_at', 'updated_at',
                        'due_on', 'starts_on', 'comments_count', 'visible_to_clients')}
            evidence.update(account_id=ACCOUNT, project_id=PROJECT,
                parent={k: task.get('parent', {}).get(k) for k in ('id', 'title', 'type')},
                creator=person(task.get('creator', {})),
                assignees=[person(p) for p in task.get('assignees', [])],
                description=safe_content(task.get('description')),
                description_attachments=[attachment(x) for x in task.get('description_attachments', [])], comments=[])
            by_comment = {}
            for c in comments:
                require(c.get('parent', {}).get('id') == task_id and c.get('bucket', {}).get('id') == PROJECT, 'comment_binding_mismatch')
                by_comment[c['id']] = c
                evidence['comments'].append({**{k:c.get(k) for k in ('id', 'created_at', 'updated_at', 'status', 'visible_to_clients')},
                    'creator':person(c.get('creator', {})), 'content':safe_content(c.get('content')),
                    'attachments':[attachment(x) for x in c.get('content_attachments', [])]})
            evidence['read_evidence'] = {'recorded_at':datetime.now(timezone.utc).isoformat(),
                'verified_identity':'hamle@gamechangers.com.tr', 'native_api':True, 'comments_complete':True,
                'pagination_pages':len(seen_urls), 'business_writes':0, 'paid_model_calls':0,
                'signed_urls_persisted':False, 'historical_reference_only':True}
            save(OUT/f'task-{task_id}'/'native-task-and-comments.json', evidence)
            for selection in [s for s in selections if s.get('task_id') == task_id]:
                c = by_comment.get(selection.get('comment_id'))
                require(c is not None, 'selected_comment_unknown')
                files = [x for x in c.get('content_attachments', []) if x.get('filename') == selection.get('filename')]
                require(len(files) == 1, 'selected_file_ambiguous')
                file = files[0]; name = file['filename']; label = selection.get('label', '')
                require(re.fullmatch('[a-z0-9-]{1,80}', label) and Path(name).name == name and '/' not in name and '\\' not in name,
                        'selected_filename_invalid')
                total += file.get('byte_size', MAX_TOTAL + 1)
                require(total <= MAX_TOTAL, 'total_download_too_large')
                data = download(session, file, access)
                folder = OUT/f'task-{task_id}'/'artifacts'/label
                folder.mkdir(parents=True, exist_ok=True)
                target = folder/name
                require(not target.exists() or target.read_bytes() == data, 'existing_local_version_conflict')
                target.write_bytes(data); target.chmod(0o600)
                downloaded.append({'task_id':task_id, 'comment_id':c['id'], 'creator':person(c['creator']),
                    'selection':label, 'attachment':attachment(file), 'path':str(target),
                    'byte_size':len(data), 'sha256':hashlib.sha256(data).hexdigest(),
                    'magic_hex':data[:12].hex(), 'source_comment_created_at':c['created_at']})
            files = [a for c in evidence['comments'] for a in c['attachments']]
            summaries.append({'task_id':task_id, 'comments':len(comments), 'attachment_count':len(files),
                'attachment_types':sorted({str(a.get('content_type')) for a in files}),
                'comment_creators':sorted({(c['creator']['id'], c['creator']['name']) for c in evidence['comments']})})
        if selections:
            require(len(downloaded) == len(selections), 'unhandled_selection')
            save(OUT/args.download_manifest, downloaded)
        save(OUT/'read-summary.json', {'tasks':summaries, 'downloaded_count_this_run':len(downloaded), 'business_writes':0, 'paid_model_calls':0})
        print(json.dumps({'tasks':summaries, 'downloaded_count_this_run':len(downloaded), 'business_writes':0, 'paid_model_calls':0}, ensure_ascii=False))
    finally:
        session.close()


if __name__ == '__main__':
    try:
        main()
    except Exception as e:
        print(json.dumps({'status':'held', 'reason':str(e) if type(e) is RuntimeError else type(e).__name__, 'secrets_printed':False}))
        raise SystemExit(2)
