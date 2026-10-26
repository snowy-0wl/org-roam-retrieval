"""Explicit model-cache provisioning; normal worker operation stays offline."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
from .embedding import DEFAULT_MODEL, DEFAULT_REVISION


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('--download', action='store_true', help='Explicitly permit fetching the pinned model snapshot')
    parser.add_argument('--revision', default=DEFAULT_REVISION)
    args = parser.parse_args()
    from .embedding import Profile
    Profile(revision=args.revision)
    from huggingface_hub import snapshot_download
    try:
        path = Path(snapshot_download(DEFAULT_MODEL, revision=args.revision,
                                     local_files_only=not args.download,
                                     allow_patterns=['*.json', '*.safetensors', '*.model', 'tokenizer.*', '*.txt']))
        present = (path / 'config.json').is_file() and any(path.glob('*.safetensors'))
        print(json.dumps({'model':DEFAULT_MODEL,'revision':args.revision,'cache_present':present,
                          'download_requested':args.download,'path':str(path)}))
        if not present:
            raise SystemExit(1)
    except Exception as error:
        print(json.dumps({'model':DEFAULT_MODEL,'revision':args.revision,'cache_present':False,'error':str(error)}))
        raise SystemExit(1)


if __name__ == '__main__':
    main()
