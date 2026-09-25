"""NVIDIA HTTPS adapter and shared priority/rate-limit/circuit scheduler."""
import concurrent.futures
from contextlib import contextmanager
import email.utils
import itertools
import json
import logging
import math
import random
import threading
import time
import urllib.error
import urllib.request

from .provider import ChatResult, ProviderError
from .diagnostics import provider_error

LOG = logging.getLogger(__name__)
PRIORITIES = {'parse': 0, 'query': 1, 'rerank': 2, 'embed_metadata': 10}


def retry_after(value):
    try:
        return max(0, float(value))
    except (ValueError, TypeError):
        try:
            return max(0, email.utils.parsedate_to_datetime(value).timestamp() - time.time())
        except (ValueError, TypeError, OverflowError):
            return 0


def complete_future(future, *, result=None, error=None):
    # The caller can abandon a running request at the exact instant it finishes.
    try:
        if error is not None:
            future.set_exception(error)
        else:
            future.set_result(result)
    except concurrent.futures.InvalidStateError:
        pass


class RemoteInferenceScheduler:
    """One worker intentionally stays below any configured concurrency ceiling.

    Delayed retries return to the queue so interactive requests can overtake
    background retries. Retry-After imposes a provider-wide cooldown.
    """
    def __init__(self, config):
        self.config = config
        self.condition = threading.Condition()
        self.queue = []
        self.sequence = itertools.count()
        self.stopped = False
        self.next_start = self.open_until = 0
        self.failures = 0
        self.state = 'ok'
        self.worker = threading.Thread(target=self._run, name='nh-inference', daemon=True)
        self.worker.start()

    def submit(self, callback, purpose, *, deadline=None):
        future = concurrent.futures.Future()
        with self.condition:
            if self.stopped:
                raise ProviderError('provider_stopped')
            if time.monotonic() < self.open_until:
                raise ProviderError('provider_degraded', transient=True, retry_after=self.open_until-time.monotonic())
            self.queue.append([PRIORITIES.get(purpose, 10), next(self.sequence), 0, 0, purpose, callback, future])
            self.condition.notify()
        try:
            return future.result(timeout=max(0, deadline-time.monotonic()) if deadline is not None else None)
        except concurrent.futures.TimeoutError:
            # Remove unsent work. An already-sent request finishes normally in the
            # worker, but the caller may immediately return local fallback.
            with self.condition:
                self.queue[:] = [task for task in self.queue if task[-1] is not future]
                future.cancel()
            raise ProviderError('interactive_deadline') from None

    def _run(self):
        while True:
            with self.condition:
                if self.stopped:
                    return
                now = time.monotonic()
                ready = [task for task in self.queue if task[2] <= now]
                wait = max(self.next_start, self.open_until) - now
                if not ready or wait > 0:
                    until = min((task[2] for task in self.queue), default=now + 60)
                    self.condition.wait(max(.01, min(60, max(wait, until-now))))
                    continue
                task = min(ready, key=lambda t: (t[0], t[1]))
                self.queue.remove(task)
                self.next_start = now + self.config['min_request_interval_ms'] / 1000
            priority, sequence, due, attempt, purpose, callback, future = task
            if future.cancelled():
                continue
            try:
                result = callback(attempt)
            except ProviderError as exc:
                if exc.transient:
                    self.failures += 1
                    self.next_start = max(self.next_start, time.monotonic() + exc.retry_after)
                    if self.failures >= 5:
                        self.open_until = time.monotonic() + 60
                self.state = 'degraded'
                # Durable background retries are owned by the indexer, not held here.
                maximum = self.config['max_retries_interactive'] if priority < 10 else 0
                if not future.done() and exc.transient and attempt < maximum and self.failures < 5:
                    task[2] = time.monotonic() + max(exc.retry_after, 2 ** attempt + random.random())
                    task[3] += 1
                    with self.condition:
                        self.queue.append(task)
                        self.condition.notify()
                elif not future.done():
                    complete_future(future, error=exc)
            except Exception:
                if not future.done():
                    complete_future(future, error=ProviderError('invalid_provider_response'))
                self.state = 'degraded'
            else:
                self.failures = 0
                self.open_until = 0
                self.state = 'ok'
                if not future.done():
                    complete_future(future, result=result)

    def close(self):
        with self.condition:
            self.stopped = True
            for task in self.queue:
                task[-1].set_exception(ProviderError('provider_stopped'))
            self.queue.clear()
            self.condition.notify_all()
        self.worker.join(timeout=1)


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        # Never forward a bearer credential to a redirect destination.
        raise ProviderError('provider_redirect_rejected', status=code)


class NvidiaNimClient:
    def __init__(self, config, env, *, opener=None):
        self.config = config
        self._key = env.get(config['api_key_env'], '').strip()
        self.last_error = None
        self.context = threading.local()
        self.configured = bool(self._key)
        self.opener = opener or urllib.request.build_opener(NoRedirect()).open
        self.scheduler = RemoteInferenceScheduler(config)

    @contextmanager
    def interactive_budget(self):
        previous = getattr(self.context, 'deadline', None)
        self.context.deadline = time.monotonic() + self.config['interactive_budget_seconds']
        try:
            yield
        finally:
            self.context.deadline = previous

    @property
    def state(self):
        return self.scheduler.state if self.configured else 'not_configured'

    def _request(self, endpoint, payload, purpose):
        if not self.configured:
            raise ProviderError('not_configured')
        interactive = PRIORITIES.get(purpose, 10) < 10
        deadline = getattr(self.context, 'deadline', None) if interactive else None
        if interactive:
            deadline = min(deadline or float('inf'), time.monotonic() + self.config['interactive_timeout_seconds'])
        if deadline is not None and deadline <= time.monotonic():
            raise ProviderError('interactive_deadline')
        encoded = json.dumps(payload, ensure_ascii=False, separators=(',', ':')).encode()
        def call(attempt):
            started = time.monotonic()
            status = None
            usage = {}
            try:
                request = urllib.request.Request(self.config['api_base'].rstrip('/') + endpoint, data=encoded,
                    headers={'Authorization': 'Bearer ' + self._key, 'Content-Type': 'application/json'})
                remaining = deadline-time.monotonic() if deadline is not None else self.config['request_timeout_seconds']
                if remaining <= 0:
                    raise ProviderError('interactive_deadline')
                with self.opener(request, timeout=max(.01, min(self.config['request_timeout_seconds'], remaining))) as response:
                    status = response.status
                    data = response.read(8 * 1024 * 1024 + 1)
                    if len(data) > 8 * 1024 * 1024:
                        raise ProviderError('response_too_large')
                    value = json.loads(data)
                    if not isinstance(value, dict):
                        raise ProviderError('invalid_provider_response')
                    raw_usage = value.get('usage', {})
                    if isinstance(raw_usage, dict):
                        usage = {k: v for k, v in raw_usage.items() if k in ('prompt_tokens', 'completion_tokens', 'total_tokens') and type(v) is int}
                    return value
            except urllib.error.HTTPError as exc:
                status = exc.code
                delay = retry_after(exc.headers.get('Retry-After'))
                exc.close()
                raise ProviderError('provider_http_error', status=status, retry_after=delay,
                                    transient=status in (429, 502, 503, 504)) from None
            except (urllib.error.URLError, TimeoutError, OSError):
                raise ProviderError('provider_unreachable', transient=True) from None
            except (ValueError, KeyError, TypeError):
                raise ProviderError('invalid_provider_response') from None
            finally:
                LOG.info('inference time=%s purpose=%s model=%s latency=%.3f status=%s retries=%s input_bytes=%s images=0 usage=%s',
                         time.time(), purpose, payload['model'], time.monotonic()-started, status, attempt, len(encoded), usage)
        try:
            result = self.scheduler.submit(call, purpose, deadline=deadline)
            self.last_error = None
            return result
        except ProviderError as exc:
            self.last_error = provider_error(exc)
            raise

    def chat(self, *, model, messages, max_tokens, temperature, purpose):
        payload = dict(model=model, messages=messages, max_tokens=max_tokens, temperature=temperature, stream=False)
        if model.startswith(('nvidia/nemotron-3.5-', 'nvidia/nemotron-3-')):
            payload['chat_template_kwargs'] = {'enable_thinking': False}
        value = self._request('/chat/completions', payload, purpose)
        try:
            text = value['choices'][0]['message']['content']
            if not isinstance(text, str):
                raise ValueError()
            return ChatResult(text, value.get('usage', {}))
        except (KeyError, IndexError, TypeError, ValueError):
            raise ProviderError('invalid_chat_response') from None

    def embed_texts(self, *, model, texts, input_type, purpose):
        if input_type not in ('query', 'passage'):
            raise ValueError('invalid embedding input type')
        value = self._request('/embeddings', dict(model=model, input=texts, input_type=input_type, encoding_format='float', truncate='END'), purpose)
        try:
            rows = sorted(value['data'], key=lambda r: r['index'])
            if [r['index'] for r in rows] != list(range(len(texts))):
                raise ValueError()
            vectors = [r['embedding'] for r in rows]
            dim = len(vectors[0])
            if not 1 <= dim <= 65536 or any(len(v) != dim or not all(type(x) in (int, float) and math.isfinite(x) for x in v) for v in vectors):
                raise ValueError()
            return vectors
        except (KeyError, TypeError, ValueError, IndexError):
            raise ProviderError('invalid_embedding_response') from None

    def close(self):
        self.scheduler.close()
