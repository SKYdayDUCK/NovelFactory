"""Archive completed experiment-branch Actions artifacts, verifying each original ZIP byte stream.

Run on the experiment checkout with GITHUB_TOKEN authorized for actions:read.
Archive creation only writes local files. Publishing is an explicit workflow step.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import shutil
from urllib.parse import urlsplit
from urllib.request import HTTPRedirectHandler, Request, build_opener
from zipfile import ZipFile


REPO = 'SKYdayDUCK/NovelFactory'
BRANCH = 'experiment/p59-database-throughput'


class NoCredentialRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        target = super().redirect_request(req, fp, code, msg, headers, newurl)
        if target is not None and urlsplit(target.full_url).netloc != urlsplit(req.full_url).netloc:
            target.remove_header('Authorization')
        return target


def request(path, binary=False):
    headers = {'Authorization': 'Bearer ' + os.environ['GITHUB_TOKEN'],
               'User-Agent': 'p59-db-evidence', 'Accept': 'application/vnd.github+json'}
    response = build_opener(NoCredentialRedirect).open(
        Request('https://api.github.com/repos/' + REPO + '/' + path, headers=headers), timeout=90)
    if binary:
        return response
    with response:
        return json.load(response)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('runs', type=int, nargs='+')
    parser.add_argument('--directory', type=Path, default=Path('reports/performance/p59_database/raw/actions'))
    args = parser.parse_args()
    for rid in args.runs:
        run = request(f'actions/runs/{rid}')
        if run['head_branch'] != BRANCH or run['head_repository']['full_name'] != REPO:
            raise ValueError(f'Unexpected source for Actions run {rid}')
        if run['status'] != 'completed':
            raise ValueError(f'Run {rid} is still {run["status"]}')
        batch = request(f'actions/runs/{rid}/artifacts?per_page=100')
        if not 0 < batch['total_count'] == len(batch['artifacts']):
            raise ValueError(f'Incomplete artifact listing for {rid}')
        directory = args.directory / str(rid)
        directory.mkdir(parents=True, exist_ok=True)
        manifest = {k: run[k] for k in ('id','name','html_url','head_sha','status','conclusion','run_attempt')}
        manifest['artifacts'] = []
        for artifact in sorted(batch['artifacts'], key=lambda a: a['id']):
            aid = artifact['id']
            path = directory / f'{aid}.zip'
            expected = artifact['digest']
            if not path.exists():
                with request(f'actions/artifacts/{aid}/zip', binary=True) as src, path.with_suffix('.part').open('wb') as dst:
                    shutil.copyfileobj(src, dst)
                path.with_suffix('.part').replace(path)
            with path.open('rb') as file:
                digest = 'sha256:' + hashlib.file_digest(file, 'sha256').hexdigest()
            if digest != expected or path.stat().st_size != artifact['size_in_bytes']:
                raise ValueError(f'Artifact {aid} digest or length mismatch')
            with ZipFile(path) as archive:
                bad = archive.testzip()
                if bad is not None:
                    raise ValueError(f'Artifact {aid} failed CRC validation')
            manifest['artifacts'].append({'name':artifact['name'], 'file':path.name, 'digest':digest,
                                          'bytes':path.stat().st_size, 'artifact_id':aid})
        (directory/'manifest.json').write_text(json.dumps(manifest, indent=2)+'\n', encoding='utf-8')
        print(f'{rid}: {len(manifest["artifacts"])} artifacts verified')


if __name__ == '__main__':
    main()
