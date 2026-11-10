"""Reuse every unchanged baseline positive/negative example through production validators."""
from pathlib import Path
import hashlib
import json

import pytest

from novel_factory.contracts._schema_bundle import SOURCE_SHA256
from novel_factory.contracts.generate import generated
from novel_factory.contracts.registry import model_for, validate_contract
from novel_factory.domain.errors import DomainError

ROOT = Path(__file__).resolve().parents[2]


def cases():
    specs = [
        ('examples/index.json', '2.0', False),
        ('invalid_examples/core_index.json', '2.0', True),
        ('examples/literary_index.json', 'literary', False),
        ('examples/r1_index.json', '2.1', False),
    ]
    result = []
    for index_file, version, only_invalid in specs:
        index_path = ROOT / 'contracts' / index_file
        content = json.loads(index_path.read_text())
        if only_invalid:
            content = {'invalid_examples': content}
        for field, invalid in [('examples', False), ('invalid_examples', True)]:
            entries = content.get(field, {})
            if only_invalid and field == 'examples':
                invalid = True
            for filename, name in entries.items():
                if isinstance(name, dict):
                    name = name.get('definition', name.get('schema_def'))
                result.append(pytest.param(version, name, index_path.parent / filename, invalid,
                                          id=f'{version}-{Path(filename).stem}'))
    return result


@pytest.mark.parametrize('version,name,path,invalid', cases())
def test_unchanged_contract_examples(version, name, path, invalid):
    payload = json.loads(path.read_text())
    if invalid:
        with pytest.raises(DomainError, match='contract constraints failed'):
            validate_contract(name, payload, version)
        with pytest.raises(ValueError):
            model_for(name, version).model_validate(payload)
    else:
        assert validate_contract(name, payload, version) == payload
        assert model_for(name, version).model_validate(payload).model_dump(mode='json') == payload


def test_generated_bundle_matches_source_and_hashes():
    for name, expected in SOURCE_SHA256.items():
        assert hashlib.sha256((ROOT / 'contracts' / name).read_bytes()).hexdigest() == expected
    assert (ROOT / 'src/novel_factory/contracts/_schema_bundle.py').read_text() == generated(ROOT)


def test_core_schema_bytes_still_match_original_manifest():
    original = json.loads((ROOT / 'validation/r1_integration_manifest.json').read_text())
    # Baseline hashes are preserved in the original integration manifest;
    # exact independent recorded values are also fixed here as a drift gate.
    for filename, expected in [
        ('core.schema.json', '472bfa809db9270b7b212969a3896f1567295c0fd68f70e94d9a404dd53ac552'),
        ('literary.schema.json', 'aea5e58465ee3ba008ab9de54002a304cddac6e405c3195201157a278c1433cc'),
        ('temporal_concurrency.schema.json', '677118bb3ea9ef7a89ac1fbd8a82a71ce611ac9efa00d67de566e7070b83d749'),
    ]:
        assert hashlib.sha256((ROOT / 'contracts' / filename).read_bytes()).hexdigest() == expected
