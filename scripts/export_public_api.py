"""Export the registered NF, Workshop and installation MCP tools and HTTP routes.

Run from a source checkout with both services' development dependencies installed.
"""
import argparse
import ast
import json
import re
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path[:0] = [str(ROOT / 'src'), str(ROOT / 'workshop/src'), str(ROOT)]

from jsonschema import Draft202012Validator, FormatChecker

from agent_workshop.server import TOOLS
from novel_factory.application.delegation import SCHEMAS
from novel_factory.contracts.registry import _REGISTRY, _source
from novel_factory.interfaces.delegation_stdio import _DESCRIPTIONS
from novel_factory.interfaces.mcp_stdio import _TOOLS


def http_routes():
    source = Path('src/novel_factory/interfaces/http.py')
    module = ast.parse((ROOT / source).read_text(encoding='utf-8'))
    factory = next(node for node in module.body if isinstance(node, ast.FunctionDef) and node.name == 'create_app')
    for handler in factory.body:
        if not isinstance(handler, ast.AsyncFunctionDef):
            continue
        for route in handler.decorator_list:
            if not (isinstance(route, ast.Call) and isinstance(route.func, ast.Attribute)
                    and isinstance(route.func.value, ast.Name) and route.func.value.id == 'app'
                    and route.func.attr in {'get', 'post', 'put', 'patch', 'delete'}):
                continue
            method = route.func.attr.upper()
            path = route.args[0].value
            entry = {'transport': 'http', 'method': method, 'path': path,
                     'source': source.as_posix(), 'handler': handler.name}
            parameters = re.findall(r'\{([^{}]+)\}', path)
            if parameters:
                entry['pathParameters'] = {param: {'type': 'string', 'format': 'uuid'} for param in parameters}
            if method == 'POST':
                # Each handler validates its actual body or explicitly constructs a prepared request.
                calls = [call for call in ast.walk(handler) if isinstance(call, ast.Call)
                         and isinstance(call.func, ast.Name)]
                schema_call = next((c for c in calls if c.func.id == '_body'), None)
                if schema_call is None:
                    schema_call = next(c for c in calls if c.func.id == 'validate_contract'
                                       and c.args[0].value != 'UUID')
                    name, version = schema_call.args[0].value, schema_call.args[2].value
                else:
                    name, version = (arg.value for arg in schema_call.args[1:3])
                entry['inputSchema'] = {'$ref': _source(name, version)['$id'] + '#/$defs/' + name}
            yield f'{method} {path}', entry


def catalog():
    entries = {}
    for name, (description, schema) in _TOOLS.items():
        entries[name] = {'transport': 'nf-mcp', 'description': description, 'inputSchema': schema,
                         'source': 'src/novel_factory/interfaces/mcp_stdio.py'}
    for name, (description, schema, handler) in TOOLS.items():
        entries[name] = {'transport': 'workshop-mcp', 'description': description, 'inputSchema': schema,
                         'source': 'workshop/src/' + handler.__module__.replace('.', '/') + '.py',
                         'handler': handler.__name__}
    for name, schema in SCHEMAS.items():
        entries[name] = {'transport': 'installation-mcp', 'description': _DESCRIPTIONS[name],
                         'inputSchema': schema, 'source': 'src/novel_factory/application/delegation.py'}
    entries.update(http_routes())
    return dict(sorted(entries.items()))


def validate_examples(path, entries):
    examples = json.loads(path.read_text(encoding='utf-8'))
    if examples.keys() != entries.keys():
        raise ValueError('Example keys differ from the registered interfaces')
    for name, example in examples.items():
        schema = entries[name].get('inputSchema', {'type': 'object', 'maxProperties': 0})
        validator = Draft202012Validator(schema, registry=_REGISTRY, format_checker=FormatChecker())
        errors = list(validator.iter_errors(example['request']))
        if errors:
            raise ValueError(f"{name}: {errors[0].message}")
        path_fields = entries[name].get('pathParameters', {})
        bindings = example.get('pathBindings', {})
        path_schema = {'type': 'object', 'properties': path_fields,
                       'required': list(path_fields), 'additionalProperties': False}
        Draft202012Validator(path_schema, format_checker=FormatChecker()).validate(bindings)
        test_path, test_name = example['test'].split('::', 1)
        source = (ROOT / test_path).read_text(encoding='utf-8')
        functions = {node.name for node in ast.walk(ast.parse(source))
                     if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))}
        if test_name.split('[')[0].split('::')[-1] not in functions:
            raise ValueError(f"{name}: source test no longer exists")
        if not isinstance(example['response'], dict):
            raise ValueError(f"{name}: expected a structured result")


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    group = parser.add_mutually_exclusive_group()
    group.add_argument('--name', help='Print one tool or METHOD /path')
    group.add_argument('--output', type=Path, help='Write the complete catalog')
    group.add_argument('--check', type=Path, help='Compare a previously exported catalog')
    parser.add_argument('--examples', type=Path, help='Validate request schemas and source test references')
    args = parser.parse_args()
    if args.examples and not args.check:
        parser.error('--examples requires --check')
    entries = catalog()
    if args.check:
        saved = json.loads(args.check.read_text(encoding='utf-8'))
        if saved != entries:
            changed = sorted(name for name in saved.keys() | entries.keys() if saved.get(name) != entries.get(name))
            parser.exit(1, 'Interface drift: ' + ', '.join(changed) + '\n')
        if args.examples:
            validate_examples(args.examples, entries)
            print(f'{len(entries)} example requests and originating tests match')
        print(f'{len(entries)} registered interfaces match')
        return
    if args.name and args.name not in entries:
        parser.error('Unknown interface')
    output = json.dumps(entries[args.name] if args.name else entries, indent=2, ensure_ascii=False) + '\n'
    if args.output:
        args.output.write_text(output, encoding='utf-8')
    else:
        sys.stdout.write(output)


if __name__ == '__main__':
    main()
