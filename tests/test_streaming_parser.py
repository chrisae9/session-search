import json
import tracemalloc

from session_search.capture.parsers.codex import CodexParser


def test_large_ignored_payloads_do_not_accumulate_in_parser_memory(tmp_path):
    path = tmp_path / 's.jsonl'
    image = 'data:image/png;base64,' + 'a' * (512 * 1024)
    with path.open('w') as stream:
        stream.write(json.dumps({'type': 'session_meta', 'payload': {'id': 's'}}) + '\n')
        stream.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message',
            'role': 'user', 'content': [{'type': 'input_text', 'text': 'retained task'}]}}) + '\n')
        for _ in range(50):
            stream.write(json.dumps({'type': 'response_item', 'payload': {'type': 'message',
                'role': 'assistant', 'content': [{'type': 'output_text', 'text': 'retained answer'},
                                                {'type': 'input_image', 'image_url': image}]}}) + '\n')
    tracemalloc.start()
    try:
        parsed = CodexParser(session_index=tmp_path / 'no-index').parse_session(path)
        _, peak = tracemalloc.get_traced_memory()
    finally:
        tracemalloc.stop()
    assert parsed.turns[0].user_text == 'retained task'
    assert parsed.turns[0].assistant_text.count('retained answer') == 50
    assert peak < 8 * 1024 * 1024
