"""Prebind Draft 2020-12 keyword evaluators for the immutable contract bundle.

Only boolean validity is public. No payload, error, permission or parsed result
enters the plan. Unknown keywords and changes to reference scope select the
ordinary validator for the complete bundle.
"""
from urllib.parse import urljoin

from jsonschema import Draft202012Validator, FormatChecker, ValidationError
from referencing.jsonschema import DRAFT202012


# The supported handlers use the validator's type checker, descend/evolve and
# format checker. Annotation-tracking and dynamically scoped refs need the
# ordinary resolver and are deliberately excluded.
_KEYWORDS = frozenset({
    'additionalProperties', 'allOf', 'anyOf', 'const', 'dependentSchemas',
    'enum', 'format', 'if', 'items', 'maxItems', 'maxLength', 'maxProperties',
    'maximum', 'minItems', 'minLength', 'minimum', 'not', 'oneOf', 'pattern',
    'prefixItems', 'properties', 'required', 'type', 'uniqueItems',
})
_METADATA = frozenset({'title', 'description', '$comment', 'default', 'examples',
                       'then', 'else', '$defs', '$ref'})


class StaticValidationPlan:
    def __init__(self, documents):
        self.nodes = {}
        self.references = {}
        self.format_checker = FormatChecker()
        self.supported = True
        for uri, root in documents.items():
            if root.get('$schema') != 'https://json-schema.org/draft/2020-12/schema':
                self.supported = False
                return
            for name, schema in root.get('$defs', {}).items():
                self.references[uri + '#/$defs/' + name] = schema
                self._register(uri, schema)
        if any(node.ref and node.ref not in self.references for node in self.nodes.values()):
            self.supported = False

    def _register(self, uri, schema):
        if not isinstance(schema, (bool, dict)):
            self.supported = False
            return
        if id(schema) in self.nodes:
            return
        if isinstance(schema, dict) and schema.keys() - _KEYWORDS - _METADATA:
            self.supported = False
        self.nodes[id(schema)] = _Node(self, uri, schema)
        for child in DRAFT202012.subresources_of(schema):
            self._register(uri, child)

    def validator(self, source, name):
        return self.nodes[id(source['$defs'][name])]


class _Node:
    # Each node refers to the same immutable schema object throughout the life
    # of the bundle. The upstream handlers inspect this API during traversal.
    __slots__ = ('plan', 'schema', 'ref', 'checks', 'format_checker')

    def __init__(self, plan, uri, schema):
        self.plan = plan
        self.schema = schema
        self.format_checker = plan.format_checker
        self.ref = urljoin(uri, schema['$ref']) if isinstance(schema, dict) and '$ref' in schema else None
        self.checks = []
        if isinstance(schema, dict):
            self.checks = [(Draft202012Validator.VALIDATORS[key], value)
                           for key, value in schema.items() if key in _KEYWORDS]

    @staticmethod
    def is_type(instance, type_name):
        return Draft202012Validator.TYPE_CHECKER.is_type(instance, type_name)

    def evolve(self, *, schema):
        return self.plan.nodes[id(schema)]

    def descend(self, instance, schema, **_path):
        yield from self.evolve(schema=schema).iter_errors(instance)

    def iter_errors(self, instance):
        if self.schema is False:
            yield ValidationError('False schema')
            return
        if self.ref is not None:
            target = self.plan.references[self.ref]
            yield from self.evolve(schema=target).iter_errors(instance)
        for check, value in self.checks:
            yield from check(self, value, instance, self.schema) or ()

    def is_valid(self, instance):
        return next(self.iter_errors(instance), None) is None
