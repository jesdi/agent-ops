"""Native ordinary input validation and the narrow supported history comparison."""
from dispatcher.runtime_work import nonempty


def valid_span(span):
    if not isinstance(span, dict) or not isinstance(span.get('byteRange'), dict):
        return False
    bounds = span['byteRange']
    return (all(type(bounds.get(k)) is int and bounds[k] >= 0 for k in ('start', 'end'))
            and (span.get('placeholder') is None or isinstance(span['placeholder'], str)))


def native_item(item):
    if not isinstance(item, dict):
        return False
    kind = item.get('type')
    if not isinstance(kind, str):
        return False
    if kind == 'text':
        spans = item.get('text_elements', [])
        return (isinstance(item.get('text'), str) and isinstance(spans, list)
                and all(valid_span(span) for span in spans))
    return native_attachment(item, kind)


def native_attachment(item, kind):
    if kind in ('image', 'localImage') and item.get('detail') not in (None, 'auto', 'low', 'high', 'original'):
        return False
    if kind == 'image':
        return isinstance(item.get('url'), str) or isinstance(item.get('fileId'), str)
    required = {'localImage': ('path',), 'audio': ('url',), 'localAudio': ('path',),
                'skill': ('name', 'path'), 'mention': ('name', 'path')}.get(kind)
    return required is not None and all(isinstance(item.get(k), str) for k in required)


def valid_native_input(provenance, root):
    if not isinstance(provenance, dict) or not nonempty(root):
        return False
    target = native_target(provenance)
    content = provenance.get('input')
    return (target and provenance.get('thread_id') == root and isinstance(content, list)
            and all(native_item(item) for item in content))


def native_target(provenance):
    method, expected = provenance.get('method'), provenance.get('expected_turn_id', '')
    return ((method == 'turn/start' and expected is None)
            or (method == 'turn/steer' and nonempty(expected)))


def text_input(content):
    if not isinstance(content, list) or not content:
        return None
    result = []
    for item in content:
        if not supported_text(item):
            return None
        result.append(item['text'])
    return result


def supported_text(item):
    return (isinstance(item, dict) and set(item) <= {'type', 'text', 'text_elements'}
            and item.get('type') == 'text' and isinstance(item.get('text'), str)
            and item.get('text_elements', []) == [])


def same_text(left, right):
    normalized = text_input(left)
    return normalized is not None and normalized == text_input(right)


def input_sources(snapshot, identity):
    """All durable sources must agree; none can repair missing legacy content."""
    sources = []
    initial = (snapshot.get('bootstrap') or {}).get('initial_input', {})
    if initial.get('client_message_id') == identity:
        sources.append(dict(input=initial['input'], expected_turn_id=None))
    native = snapshot['inputs'].get(identity, {}).get('native_input')
    if native is not None:
        sources.append(native)
    for batch in snapshot['deliveries']:
        for attempt in batch['attempts']:
            if attempt['client_message_id'] == identity:
                sources.append(dict(input=batch['input'], expected_turn_id=attempt['expected_turn_id']))
    return sources


def matches_input(snapshot, identity, turn, content):
    sources = input_sources(snapshot, identity)
    return bool(sources) and all(same_text(source['input'], content)
        and source['expected_turn_id'] in (None, turn) for source in sources)
