"""Bounded, secret-filtered reads of user-selected WAT example tasks.

Uses existing Hamle CLI/keychain profile through verify_wat_read_only.read.
No provider write commands or paid model calls. Each optional download must be
one named attachment on a comment already returned for the selected task.
"""
import argparse
import datetime
import hashlib
from html.parser import HTMLParser
import json
from pathlib import Path
import re
from urllib.parse import urlsplit

from verify_wat_read_only import read, ROOT

ALLOWED_TASKS = {'10309337809', '10064527849'}


class ContentParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.parts, self.attachments, self.links = [], [], []

    def handle_data(self, data):
        self.parts.append(data)

    def handle_starttag(self, tag, attrs):
        values = dict(attrs)
        if tag in ('br', 'p', 'div', 'li'):
            self.parts.append('\n')
        if tag == 'bc-attachment' and values.get('content-type') != 'application/vnd.basecamp.mention':
            self.attachments.append({k: v for k, v in values.items()
                if k in ('filename', 'content-type', 'filesize', 'title', 'width', 'height', 'caption', 'id')})
        if tag == 'a' and values.get('href'):
            u = urlsplit(values['href'])
            self.links.append({'host': u.netloc, 'path': u.path, 'has_query': bool(u.query)})


def content(raw):
    parser = ContentParser()
    parser.feed(raw or '')
    text = re.sub(r'https?://\S+', '[linked URL]', ''.join(parser.parts))
    return {'text': re.sub(r'\n{3,}', '\n\n', text).strip(),
            'attachments': parser.attachments, 'link_metadata': parser.links}


def person(data):
    return {k: data.get(k) for k in ('id', 'name', 'title', 'employee')}


def save(path, value):
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + '\n')
    path.chmod(0o600)


def run(args):
    if args.task not in ALLOWED_TASKS:
        raise SystemExit('task_not_explicitly_authorized')
    out = ROOT / 'outputs/wat-pilot-20261010' / ('example-' + args.task)
    out.mkdir(parents=True, exist_ok=True)
    identity = read(['auth', 'status', '--check'])
    data = identity.get('data', {})
    if not (identity.get('success') and data.get('storage') == 'keyring'
            and data.get('valid') is True and data.get('user_email') == 'hamle@gamechangers.com.tr'
            and str(data.get('account_id')) == '5898865'):
        raise SystemExit('identity_check_failed')
    result = read(['todos', 'show', 'https://app.basecamp.com/5898865/buckets/40694338/todos/' + args.task,
                   '--comments'])
    if not result.get('success'):
        print(json.dumps(result))
        return
    data = result['data']
    if str(data.get('id')) != args.task or data.get('bucket', {}).get('id') != 40694338:
        raise SystemExit('task_binding_mismatch')
    evidence = {k: data.get(k) for k in ('id', 'title', 'status', 'completed', 'created_at', 'updated_at',
                'due_on', 'starts_on', 'comments_count', 'visible_to_clients')}
    evidence.update(bucket=data.get('bucket'),
        parent={k: data.get('parent', {}).get(k) for k in ('id', 'title', 'type')},
        creator=person(data.get('creator', {})),
        assignees=[person(p) for p in data.get('assignees', [])],
        description=content(data.get('description')), comments=[])
    for comment in data.get('comments', []):
        evidence['comments'].append({**{k: comment.get(k) for k in ('id', 'created_at', 'updated_at',
            'visible_to_clients', 'status')}, 'creator': person(comment.get('creator', {})),
            'content': content(comment.get('content'))})
    evidence.update(observed_at=datetime.datetime.now(datetime.timezone.utc).isoformat(),
                    identity_verified=True, reads_only=True, network_writes=0, paid_model_calls=0,
                    fetched_comment_count=len(evidence['comments']))
    evidence['comments_complete'] = evidence['fetched_comment_count'] == evidence['comments_count']
    save(out / 'task-and-comments-read-evidence.json', evidence)
    if args.download_comment:
        if not args.file or not args.label or not re.fullmatch(r'[a-z0-9-]+', args.label):
            raise SystemExit('download_requires_exact_file_and_safe_label')
        comments = [c for c in evidence['comments'] if str(c['id']) == args.download_comment]
        if len(comments) != 1 or args.file not in [a.get('filename') for a in comments[0]['content']['attachments']]:
            raise SystemExit('attachment_not_in_selected_task')
        target = out / 'artifacts' / args.label
        target.mkdir(parents=True, exist_ok=True)
        result = read(['attachments', 'download',
            'https://app.basecamp.com/5898865/buckets/40694338/comments/' + args.download_comment,
            '--type', 'comment', '--file', args.file, '--out', str(target)])
        files = []
        for path in target.iterdir():
            if path.is_file():
                path.chmod(0o600)
                files.append({'filename': path.name, 'local_path': str(path), 'bytes': path.stat().st_size,
                              'sha256': hashlib.sha256(path.read_bytes()).hexdigest()})
        summary = {'comment_id': args.download_comment, 'selection': args.label,
                   'success': result['success'], 'reason': result.get('reason'), 'files': files}
        save(out / ('download-' + args.label + '.json'), summary)
        print(json.dumps(summary, ensure_ascii=False))
    else:
        print(json.dumps(evidence, ensure_ascii=False))


if __name__ == '__main__':
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--task', required=True)
    parser.add_argument('--download-comment')
    parser.add_argument('--file')
    parser.add_argument('--label')
    run(parser.parse_args())
