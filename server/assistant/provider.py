"""Provider-neutral interface and a deterministic offline test provider."""
from dataclasses import dataclass, field
from typing import Protocol
import hashlib
import json

@dataclass
class ChatResult:
    text: str
    usage: dict = field(default_factory=dict)

class ProviderError(Exception):
    def __init__(self, code, *, status=None, retry_after=0, transient=False):
        super().__init__(code)
        self.code, self.status = code, status
        self.retry_after, self.transient = retry_after, transient

class ModelProvider(Protocol):
    def chat(self, *, model: str, messages: list[dict[str, object]], max_tokens: int, temperature: float, purpose: str) -> ChatResult: ...
    def embed_texts(self, *, model: str, texts: list[str], input_type: str, purpose: str) -> list[list[float]]: ...
    def visual_chat(self, *, model: str, messages: list[dict[str, object]], max_tokens: int) -> ChatResult: ...

class FakeModelProvider:
    """Injected replies/errors and call recording; never accesses the network."""
    configured = True
    state = 'ok'
    def __init__(self, replies=None):
        self.replies = list(replies or [])
        self.calls = []
    def chat(self, **kwargs):
        self.calls.append(kwargs)
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return ChatResult(reply if isinstance(reply, str) else json.dumps(reply))
        data = json.loads(kwargs['messages'][-1]['content'])
        if 'candidates' in data:
            return ChatResult(json.dumps({'results': [{'id': r['id']} for r in data['candidates'][:5]]}))
        return ChatResult(json.dumps((data.get('previous_plan') or {}) | {'semantic_query': data.get('message', '')}))
    def embed_texts(self, **kwargs):
        self.calls.append(kwargs)
        return [[(x - 127) / 128 for x in hashlib.sha256(t.encode()).digest()[:8]] for t in kwargs['texts']]
    def visual_chat(self, **kwargs):
        self.calls.append({'purpose': 'visual', **kwargs})
        if self.replies:
            reply = self.replies.pop(0)
            if isinstance(reply, Exception):
                raise reply
            return ChatResult(reply if isinstance(reply, str) else json.dumps(reply))
        return ChatResult(json.dumps({'style': ['black-and-white manga'], 'warnings': ['sampled pages only']}))
    def close(self):
        pass
