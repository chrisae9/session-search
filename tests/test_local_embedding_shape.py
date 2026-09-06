import hashlib
import sys
from types import SimpleNamespace

import pytest

from session_search.core.embeddings import EmbeddingIdentity, LocalEmbedder


def test_local_loader_requires_sequence_vector_and_disables_truncation(tmp_path, monkeypatch):
    path = tmp_path / 'synthetic.gguf'
    path.write_bytes(b'synthetic model stub')
    identity = EmbeddingIdentity('sha256:' + hashlib.sha256(path.read_bytes()).hexdigest(), dimensions=2)
    calls = []
    output = [3.0, 4.0]

    class Model:
        def __init__(self, **kwargs):
            assert kwargs['n_batch'] == kwargs['n_ctx'] == kwargs['n_ubatch'] == 8192
            assert kwargs['embedding'] is True

        def embed(self, text, *, truncate):
            calls.append((text, truncate))
            return output

    monkeypatch.setitem(sys.modules, 'llama_cpp', SimpleNamespace(Llama=Model))
    provider = LocalEmbedder(path, identity)
    assert provider.embed('query', query=True) == pytest.approx([0.6, 0.8])
    assert calls == [(identity.query_instruction + 'query', False)]
    output = [[3.0, 4.0], [4.0, 3.0]]
    with pytest.raises(ValueError, match='pooled sequence'):
        provider.embed('token vectors')
