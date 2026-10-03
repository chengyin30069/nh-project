"""Validated assistant settings; secrets are resolved only by the provider."""
from urllib.parse import urlparse

DEFAULTS = dict(
    enabled=False, provider="nvidia_nim", api_base="https://integrate.api.nvidia.com/v1",
    api_key_env="NVIDIA_API_KEY", parser_model="nvidia/nemotron-3.5-lightning-30b-a3b",
    quality_model="nvidia/nemotron-3-super-120b-a12b", embedding_model="nvidia/nemotron-3-embed-1b",
    visual_model="z-ai/glm-5.3-flash", result_limit=5, dense_candidate_count=80,
    rerank_candidate_count=12, request_timeout_seconds=75, max_retries_interactive=0,
    rerank_enabled=False, interactive_timeout_seconds=20, interactive_budget_seconds=40,
    max_retries_background=6, background_enabled=True, max_remote_concurrency=1,
    min_request_interval_ms=1600, remote_image_analysis_enabled=False,
    max_remote_image_edge=896, max_remote_image_bytes=524288,
    visual_index_on_new_gallery=False, visual_lazy_index_enabled=False,
    visual_candidate_count=80, max_remote_images_per_request=6,
    max_remote_request_bytes=5242880, visual_request_timeout_seconds=180,
)


def settings(value=None):
    value = value or {}
    if not isinstance(value, dict) or set(value) - DEFAULTS.keys():
        raise ValueError("unknown assistant settings")
    result = DEFAULTS | value
    for key, default in DEFAULTS.items():
        item = result[key]
        if type(item) is not type(default):
            raise ValueError(f"assistant.{key} must be {type(default).__name__}")
        if isinstance(default, str) and not item.strip():
            raise ValueError(f"assistant.{key} must not be empty")
        if type(default) is int and not (0 if key.startswith('max_retries') or key == 'min_request_interval_ms' else 1) <= item <= 10000000:
            raise ValueError(f"assistant.{key} is out of range")
    if result['provider'] != 'nvidia_nim':
        raise ValueError("assistant.provider must be nvidia_nim")
    # Earlier V2 examples used the NVIDIA page slug instead of its API model ID.
    if result['visual_model'] == 'z-ai/glm-5-3-flash':
        result['visual_model'] = 'z-ai/glm-5.3-flash'
    if result['visual_lazy_index_enabled']:
        raise ValueError('assistant.visual_lazy_index_enabled is not available; ordinary searches cannot schedule uploads')
    parsed = urlparse(result['api_base'])
    if parsed.scheme != 'https' or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ValueError("assistant.api_base must be an HTTPS URL without credentials or query")
    for key, cap in [('result_limit', 5), ('dense_candidate_count', 1000), ('rerank_candidate_count', 100), ('max_remote_concurrency', 8), ('max_retries_interactive', 3), ('max_retries_background', 20), ('request_timeout_seconds', 300), ('interactive_timeout_seconds', 60), ('interactive_budget_seconds', 120)]:
        if result[key] > cap:
            raise ValueError(f"assistant.{key} must be at most {cap}")
    for key, cap in [('visual_candidate_count', 1000), ('max_remote_images_per_request', 6),
                     ('max_remote_request_bytes', 5242880), ('visual_request_timeout_seconds', 300),
                     ('max_remote_image_edge', 896), ('max_remote_image_bytes', 524288)]:
        if result[key] > cap:
            raise ValueError(f"assistant.{key} must be at most {cap}")
    return result
