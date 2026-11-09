"""Publish charts to R2 and pin CSV evidence to a verified public GitHub commit."""
from __future__ import annotations

import argparse
import csv
import hashlib
import io
import json
import os
import re
import subprocess
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .archive import ArchiveError, CODE, digest, iso_date, json_bytes, write_json

ROOT = Path(__file__).resolve().parents[1]
GITHUB_REPO = 'Microhx/dipmethod-public-data'


def publication(root: Path, selected: str | None = None, *, github_commit: str):
    """Build indexes from archived identities, never infer a next trading day."""
    if not re.fullmatch(r'[a-f0-9]{40}', github_commit):
        raise ArchiveError('GitHub 引用必须解析为完整提交 ID')
    objects = {}
    indexes = {}
    for daily in sorted((root / 'data').glob('????-??-??')):
        day = iso_date(daily.name)
        if selected and day != selected:
            continue
        signals = list(csv.DictReader(io.StringIO((daily / 'signals.csv').read_text(encoding='utf-8-sig'))))
        records = []
        for signal in signals:
            record = {k: signal[k] for k in ('id', 'sig_date', 'code', 'name', 'cost')}
            record['days'] = []
            code = signal['code']
            if CODE.fullmatch(code):
                stock = daily / code
                meta = json.loads((stock / 'metadata.json').read_text())
                if (meta.get('code') != code or meta.get('signal_date') != day
                        or signal['sig_date'] != day or signal['name'] not in meta.get('names', [])
                        or signal['id'] not in meta['signal_ids'] or float(signal['cost']) not in meta['buy_points']):
                    raise ArchiveError(f'归档买点或信号 ID 不一致：{day}/{code}')
                record['next_trade_date'] = meta.get('next_trade_date')
                for chart_date in (day, meta.get('next_trade_date')):
                    if not chart_date:
                        continue
                    source = next((d for d in meta['days'] if d['date'] == chart_date), {})
                    item = {k: source[k] for k in ('date', 'status', 'rows', 'missing_labels') if k in source}
                    item['date'] = iso_date(chart_date)
                    for extension in ('png', 'csv'):
                        path = stock / f'{chart_date}.{extension}'
                        if not path.exists() or not source.get('rows'):
                            continue
                        content = path.read_bytes()
                        sha = digest(content)
                        if extension == 'csv' and sha != source.get('sha256'):
                            raise ArchiveError(f'CSV 校验失败：{day}/{code}/{chart_date}')
                        if extension == 'csv':
                            item['csv'] = {
                                'repo': GITHUB_REPO, 'commit': github_commit,
                                'path': path.relative_to(root).as_posix(), 'sha256': sha,
                            }
                            continue
                        key = f'intraday/{day}/{code}/{chart_date}-{sha[:20]}.{extension}'
                        objects[key] = (path, sha)
                        item[extension] = key
                    record['days'].append(item)
            records.append(record)
        indexes[day] = {'version': 1, 'date': day, 'signals': records}
    if selected and not indexes:
        raise ArchiveError('该日期没有本地归档')
    return objects, indexes


def github_commit(root: Path, ref: str) -> str:
    result = subprocess.run(['git', 'rev-parse', '--verify', '--end-of-options', ref + '^{commit}'],
                            cwd=root, capture_output=True, text=True)
    commit = result.stdout.strip()
    if result.returncode or not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ArchiveError('无法解析 GitHub 提交；请检查 --github-ref')
    return commit


def github_blobs(root: Path, commit: str) -> dict:
    """Fetch anonymously without changing local refs; avoid GitHub REST rate limits."""
    if not re.fullmatch(r'[a-f0-9]{40}', commit):
        raise ArchiveError('GitHub 提交 ID 无效')
    fetched = subprocess.run([
        'git', '-c', 'credential.helper=', '-c', 'core.askPass=', '-c', 'http.extraHeader=',
        'fetch', '--no-tags', '--no-write-fetch-head', f'https://github.com/{GITHUB_REPO}.git', commit,
    ], cwd=root, capture_output=True, env={**os.environ, 'GIT_TERMINAL_PROMPT': '0'}, timeout=120)
    if fetched.returncode:
        raise ArchiveError('无法从公开 GitHub 仓库读取该提交；请确认数据已推送且仓库可访问')
    tree = subprocess.run(['git', 'ls-tree', '-r', '-z', commit, '--', 'data'],
                          cwd=root, capture_output=True, timeout=30)
    if tree.returncode:
        raise ArchiveError('无法读取 GitHub 提交的文件树')
    blobs = {}
    for entry in tree.stdout.split(b'\0'):
        if not entry:
            continue
        metadata, path = entry.split(b'\t', 1)
        _, kind, sha = metadata.split()
        if kind == b'blob':
            blobs[path.decode('utf-8')] = sha.decode('ascii')
    return blobs


def verify_github(root: Path, indexes: dict, commit: str) -> int:
    """The public Git tree proves every pinned CSV exists with exactly these bytes."""
    blobs = github_blobs(root, commit)
    checked = set()
    for index in indexes.values():
        for signal in index['signals']:
            for day in signal['days']:
                reference = day.get('csv')
                if not reference:
                    continue
                path = reference['path']
                content = (root / path).read_bytes()
                if digest(content) != reference['sha256']:
                    raise ArchiveError(f'发布过程中 CSV 发生变化：{path}')
                blob = hashlib.sha1(b'blob ' + str(len(content)).encode() + b'\0' + content).hexdigest()
                if blobs.get(path) != blob:
                    raise ArchiveError(f'GitHub CSV 缺失或内容不一致，先提交并推送数据：{path}')
                checked.add(path)
    return len(checked)


def credentials(path: Path | None) -> str:
    token = os.environ.get('CLOUDFLARE_API_TOKEN')
    if token:
        return token
    candidates = [path] if path else [
        Path.home() / 'Library/Preferences/.wrangler/config/default.toml',
        Path.home() / '.config/.wrangler/config/default.toml',
    ]
    for candidate in candidates:
        if candidate and candidate.is_file():
            match = re.search(r'^oauth_token\s*=\s*"([^"\n]+)"', candidate.read_text(), re.M)
            if match:
                return match[1]
    raise ArchiveError('没有可用的 Cloudflare 凭证；先使用 Wrangler 登录或配置 CLOUDFLARE_API_TOKEN')


def transfer(url: str, *, content: bytes | None = None, headers=None) -> bytes:
    for attempt in range(5):
        try:
            request = urllib.request.Request(url, data=content, headers={
                'User-Agent': 'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/130.0.0.0 Safari/537.36',
                **(headers or {}),
            }, method='PUT' if content is not None else 'GET')
            with urllib.request.urlopen(request, timeout=45) as response:
                return response.read()
        except urllib.error.HTTPError as error:
            if error.code not in (408, 429, 500, 502, 503, 504) or attempt == 4:
                raise ArchiveError(f'文件请求失败，HTTP {error.code}') from None
        except (urllib.error.URLError, TimeoutError, ConnectionError):
            if attempt == 4:
                raise ArchiveError('R2/CDN 网络请求失败') from None
        time.sleep(min(2 ** attempt, 16))
    raise ArchiveError('R2/CDN 请求失败')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--date', type=iso_date)
    parser.add_argument('--account-id', required=True)
    parser.add_argument('--bucket', default='a-stock-images')
    parser.add_argument('--cdn', default='https://cdn.dipmethod.com')
    parser.add_argument('--wrangler-auth', type=Path)
    parser.add_argument('--github-ref', default='HEAD', help='已推送到公开仓库的 Git 引用；索引固定到完整提交 ID')
    parser.add_argument('--upload', action='store_true', help='默认仅生成发布计划；此选项执行上传')
    parser.add_argument('--jobs', type=int, default=6)
    args = parser.parse_args()
    try:
        if not re.fullmatch(r'[a-f0-9]{32}', args.account_id) or not re.fullmatch(r'[a-z0-9-]+', args.bucket):
            raise ArchiveError('账户 ID 或桶名称格式无效')
        if not args.cdn.startswith('https://'):
            raise ArchiveError('CDN 必须使用 HTTPS')
        commit = github_commit(ROOT, args.github_ref)
        objects, indexes = publication(ROOT, args.date, github_commit=commit)
        print(f'发布计划：{len(objects)} 张图片，{len(indexes)} 个日索引；CSV 固定到 GitHub {commit}', flush=True)
        plan = ROOT / '.cache/publication'
        for day, index in indexes.items():
            write_json(plan / f'{day}.json', index)
        if not args.upload:
            return 0
        verified = verify_github(ROOT, indexes, commit)
        print(f'GitHub 校验通过：{verified} 份 CSV 与本地内容一致', flush=True)
        token = credentials(args.wrangler_auth)
        api = f'https://api.cloudflare.com/client/v4/accounts/{args.account_id}/r2/buckets/{args.bucket}/objects/'
        state_path = ROOT / '.cache/r2-publish.json'
        state = json.loads(state_path.read_text()) if state_path.exists() else {}
        target = f'{args.account_id}/{args.bucket}/{args.cdn.rstrip("/")}'
        completed = state.setdefault(target, {})

        def put(key, content, content_type, cache):
            transfer(api + urllib.parse.quote(key, safe='/'), content=content, headers={
                'Authorization': f'Bearer {token}', 'Content-Type': content_type,
                'Cache-Control': cache, 'cf-r2-data-catalog-check': 'true',
            })

        def upload(item):
            key, (path, sha) = item
            if completed.get(key) == sha:
                return key, sha, True
            content = path.read_bytes()
            if digest(content) != sha:
                raise ArchiveError('发布过程中本地文件发生变化')
            put(key, content, 'image/png', 'public, max-age=31536000, immutable')
            if digest(transfer(args.cdn.rstrip('/') + '/' + key)) != sha:
                raise ArchiveError(f'CDN 文件校验失败：{key}')
            return key, sha, False

        count = skipped = 0
        with ThreadPoolExecutor(max_workers=max(1, min(args.jobs, 8))) as pool:
            futures = [pool.submit(upload, item) for item in objects.items()]
            try:
                for future in as_completed(futures):
                    key, sha, reused = future.result()
                    completed[key] = sha
                    count += 1
                    skipped += reused
                    write_json(state_path, state)
                    if count % 50 == 0 or count == len(objects):
                        print(f'文件完成 {count}/{len(objects)}，其中已校验复用 {skipped}', flush=True)
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        # Indexes become visible only once every referenced object has passed a CDN hash check.
        def publish_index(item):
            day, index = item
            key = f'intraday/manifests/{day}.json'
            content = json_bytes(index)
            put(key, content, 'application/json; charset=utf-8', 'no-store')
            if digest(transfer(api + key, headers={'Authorization': f'Bearer {token}'})) != digest(content):
                raise ArchiveError(f'日索引校验失败：{day}')
            return day

        with ThreadPoolExecutor(max_workers=max(1, min(args.jobs, 8))) as pool:
            futures = [pool.submit(publish_index, item) for item in indexes.items()]
            try:
                for count, future in enumerate(as_completed(futures), 1):
                    future.result()
                    if count % 20 == 0 or count == len(indexes):
                        print(f'日索引完成 {count}/{len(indexes)}', flush=True)
            except BaseException:
                for future in futures:
                    future.cancel()
                raise
        print(f'发布完成：{len(objects)} 张图片、{len(indexes)} 个日索引；CSV 使用已校验的 GitHub 固定提交链接。', flush=True)
        return 0
    except ArchiveError as error:
        print(str(error), file=sys.stderr)
        return 1


if __name__ == '__main__':
    raise SystemExit(main())
