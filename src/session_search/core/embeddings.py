"""Optional embedding providers with explicit identities and no automatic downloads."""

from __future__ import annotations

import hashlib
import json
import math
import threading
import urllib.request
from dataclasses import asdict, dataclass
from pathlib import Path

from session_search.core.records import canonical_json, digest
from session_search.interfaces.client import NoRedirect, validate_endpoint

QUERY_INSTRUCTION = (
    "Instruct: Given a search query, retrieve relevant conversation "
    "excerpts that answer the query\nQuery: "
)


@dataclass(frozen=True)
class EmbeddingIdentity:
    artifact: str
    dimensions: int = 1024
    preprocessing: str = "event-chars-6000-overlap-300-v1"
    normalization: str = "l2-float32-v1"
    query_instruction: str = QUERY_INSTRUCTION

    def __post_init__(self):
        if not self.artifact or not 1 <= self.dimensions <= 8192:
            raise ValueError("embedding artifact and valid dimensions are required")

    @property
    def key(self):
        return digest(canonical_json(asdict(self)).encode())


def validate_vector(vector, dimensions):
    values = [float(value) for value in vector]
    if len(values) != dimensions or not all(math.isfinite(value) for value in values):
        raise ValueError("incompatible or invalid embedding vector")
    norm = math.sqrt(sum(value * value for value in values))
    if norm == 0:
        raise ValueError("zero embedding vector")
    return [value / norm for value in values]


class RemoteEmbedder:
    def __init__(self, endpoint: str, identity: EmbeddingIdentity, *, model: str,
                 response_model: str, token_file: Path | None = None, timeout: float = 10):
        self.endpoint = validate_endpoint(endpoint)
        self.identity = identity
        self.model = model
        self.response_model = response_model
        self.token_file = token_file
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())
        # Limit this worker to two background requests; foreground requests do
        # not wait on this gate. Deployment-wide scheduling remains a server concern.
        self.background = threading.BoundedSemaphore(2)

    def embed(self, text: str, *, query: bool = False):
        headers = {"Content-Type": "application/json"}
        if self.token_file:
            headers["Authorization"] = "Bearer " + self.token_file.read_text().strip()
        payload = {"model": self.model, "input": [
            self.identity.query_instruction + text if query else text
        ]}
        request = urllib.request.Request(self.endpoint + "/embeddings",
                                         data=canonical_json(payload).encode(), headers=headers)
        if not query:
            self.background.acquire()
        try:
            with self.opener.open(request, timeout=self.timeout) as response:
                body = response.read(1024 * 1024 + 1)
                if len(body) > 1024 * 1024:
                    raise ValueError("embedding response exceeds limit")
                data = json.loads(body)
            if data.get("model") != self.response_model:
                raise ValueError("embedding response model changed")
            if len(data.get("data", [])) != 1:
                raise ValueError("embedding response count mismatch")
            return validate_vector(data["data"][0]["embedding"], self.identity.dimensions)
        finally:
            if not query:
                self.background.release()


class LocalEmbedder:
    def __init__(self, model_path: Path, identity: EmbeddingIdentity):
        self.identity = identity
        self.model_path = model_path
        self.model = None
        self.lock = threading.Lock()

    def embed(self, text: str, *, query: bool = False):
        with self.lock:
            if self.model is None:
                if not self.model_path.is_file():
                    raise FileNotFoundError("provision the local embedding model explicitly")
                with self.model_path.open("rb") as f:
                    observed = "sha256:" + hashlib.file_digest(f, "sha256").hexdigest()
                if observed != self.identity.artifact:
                    raise ValueError("local model digest differs from configured identity")
                from llama_cpp import Llama
                self.model = Llama(model_path=str(self.model_path), embedding=True,
                                   n_ctx=8192, n_batch=8192, n_ubatch=8192,
                                   n_gpu_layers=-1, verbose=False)
            result = self.model.embed(self.identity.query_instruction + text if query else text,
                                      truncate=False)
            if result and isinstance(result[0], list):
                raise ValueError("local model must return a pooled sequence embedding")
            return validate_vector(result, self.identity.dimensions)


def load_provider(config_path: Path | None, *, allow_remote: bool = False):
    if config_path is None:
        return None
    config = json.loads(config_path.read_text())
    identity = EmbeddingIdentity(**config["identity"])
    if config["mode"] == "local":
        return LocalEmbedder(Path(config["model_path"]).expanduser(), identity)
    if config["mode"] == "remote" and allow_remote:
        return RemoteEmbedder(
            config["endpoint"], identity, model=config["model"],
            response_model=config["response_model"],
            token_file=Path(config["token_file"]).expanduser() if config.get("token_file") else None,
        )
    raise ValueError("remote embeddings require explicit network authorization")
