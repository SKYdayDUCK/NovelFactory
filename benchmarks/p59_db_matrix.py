"""Run one sequential comparison block. Each child seeds a new database and object root."""
import argparse
import gzip
import json
from pathlib import Path
import subprocess
import sys

MODES = ('baseline', 'queues', 'sql', 'micro', 'micro_queues', 'hybrid')


def settings(profile):
    if profile == 'grid':
        for window in (0, 1, 2, 5, 10):
            sizes = (1, 8, 16, 32, 64, 128)
            for batch in sizes if window % 2 == 0 else reversed(sizes):
                yield 'hybrid', 50, 480, 64, batch, window, 10
    elif profile in ('mixed', 'reads'):
        for mode in (*MODES, *reversed(MODES)):
            yield mode, 50, 1000, 64, 32, 2, 10 if profile == 'mixed' else 0
    elif profile == 'adaptive':
        for concurrency in (1, 8, 64, 128):
            for mode in ('micro', 'adaptive', 'adaptive', 'micro'):
                yield mode, 50, 1000, concurrency, 32, 2, 10
    elif profile == 'pressure':
        for concurrency in (1, 8, 32, 128):
            for mode in ('baseline', 'hybrid', 'hybrid', 'baseline'):
                yield mode, 50, 400, concurrency, 32, 2, 10


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--profile', choices=['mixed', 'reads', 'grid', 'pressure', 'adaptive'], required=True)
    parser.add_argument('--directory', required=True, type=Path)
    args = parser.parse_args()
    args.directory.mkdir(parents=True, exist_ok=False)
    summary = []
    for i, (mode, agents, count, concurrency, batch, window, write_every) in enumerate(settings(args.profile)):
        path = args.directory / f'{i:02d}-{mode}.json'
        command = [sys.executable, '-m', 'benchmarks.p59_db_candidates', '--mode', mode,
                   '--agents', str(agents), '--requests', str(count), '--concurrency', str(concurrency),
                   '--workers', '4', '--capacity', '128', '--batch', str(batch), '--window-ms', str(window),
                   '--write-every', str(write_every), '--output', str(path)]
        status = subprocess.run(command).returncode
        row = {'file': path.name+'.gz', 'command': command[1:], 'exit': status}
        if path.exists():
            data = path.read_bytes()
            result = json.loads(data)
            row.update({k: result[k] for k in ('mode','goodput','requests','concurrency','window_ms','requested_batch',
                                                'latency','outcomes','latency_by_kind','sql','queue','batch_sizes')})
            path.with_suffix('.json.gz').write_bytes(gzip.compress(data, mtime=0))
            path.unlink()
        summary.append(row)
        (args.directory / 'summary.json').write_text(json.dumps(summary, indent=2)+'\n', encoding='utf-8')
    return int(any(row['exit'] for row in summary))


if __name__ == '__main__':
    raise SystemExit(main())
