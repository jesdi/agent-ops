"""Strict evaluator for the keywords in the selected generated draft-07 schemas.

Not a general-purpose JSON Schema implementation. Unknown keywords or nonlocal refs
fail loudly; format is an annotation as in default draft-07 validators.
"""
import json
from pathlib import Path

ANNOTATIONS = {'$schema', 'title', 'description', 'default', 'format'}
KEYWORDS = ANNOTATIONS | {'definitions', '$ref', 'type', 'enum', 'properties',
    'required', 'additionalProperties', 'items', 'minimum', 'minLength',
    'oneOf', 'anyOf', 'allOf'}

class SchemaError(ValueError):
    pass


def check(value, schema, root=None, where='$'):
    root = schema if root is None else root
    if schema is True:
        return
    if schema is False:
        raise SchemaError(where + ': false schema')
    unknown = set(schema) - KEYWORDS
    if unknown:
        raise SchemaError('unsupported keywords: ' + repr(sorted(unknown)))
    if '$ref' in schema:
        ref = schema['$ref']
        if not ref.startswith('#/'):
            raise SchemaError('nonlocal reference: ' + ref)
        target = root
        for part in ref[2:].split('/'):
            target = target[part.replace('~1', '/').replace('~0', '~')]
        check(value, target, root, where)
        return
    for keyword in ['allOf', 'anyOf', 'oneOf']:
        if keyword not in schema:
            continue
        successes = 0
        for branch in schema[keyword]:
            try:
                check(value, branch, root, where)
                successes += 1
            except SchemaError:
                pass
        required = len(schema[keyword]) if keyword == 'allOf' else 1
        if (keyword == 'anyOf' and successes < 1) or (keyword != 'anyOf' and successes != required):
            raise SchemaError(where + ': failed ' + keyword)
    types = schema.get('type', [])
    types = [types] if isinstance(types, str) else types
    matches = {'null': value is None, 'boolean': isinstance(value, bool),
        'integer': isinstance(value, int) and not isinstance(value, bool),
        'number': isinstance(value, (int, float)) and not isinstance(value, bool),
        'string': isinstance(value, str), 'array': isinstance(value, list),
        'object': isinstance(value, dict)}
    if types and not any(matches[t] for t in types):
        raise SchemaError(where + ': type ' + repr(types))
    if 'enum' in schema and value not in schema['enum']:
        raise SchemaError(where + ': enum')
    if isinstance(value, dict):
        missing = set(schema.get('required', [])) - set(value)
        if missing:
            raise SchemaError(where + ': missing ' + repr(sorted(missing)))
        properties = schema.get('properties', {})
        for key, child in value.items():
            check(child, properties.get(key, schema.get('additionalProperties', True)), root, where+'.'+key)
    if isinstance(value, list) and 'items' in schema:
        for index, child in enumerate(value):
            check(child, schema['items'], root, where+'['+str(index)+']')
    if isinstance(value, str) and len(value) < schema.get('minLength', 0):
        raise SchemaError(where + ': minLength')
    if isinstance(value, (int, float)) and not isinstance(value, bool) and 'minimum' in schema and value < schema['minimum']:
        raise SchemaError(where + ': minimum')


class Schemas:
    def __init__(self, directory):
        self.directory = Path(directory)
        self.loaded = {}
        self.counts = {}

    def validate(self, name, value):
        if name not in self.loaded:
            self.loaded[name] = json.loads((self.directory / name).read_text())
        check(value, self.loaded[name])
        self.counts[name] = self.counts.get(name, 0) + 1
