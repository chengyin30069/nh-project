"""Secret-free assistant status, available even when optional imports fail."""

def initialization_error(exc):
    if isinstance(exc, ModuleNotFoundError):
        return {'code': 'missing_dependency', 'message': 'Assistant dependency is missing. Install requirements-server.txt or rebuild the Docker image.'}
    if isinstance(exc, PermissionError):
        return {'code': 'storage_permission_denied', 'message': 'Assistant cannot write its database. Check storage ownership and the container UID/GID.'}
    import sqlite3
    if isinstance(exc, sqlite3.Error):
        return {'code': 'database_unavailable', 'message': 'Assistant database could not be opened. Check storage permissions, free space and database locks.'}
    if isinstance(exc, ValueError):
        return {'code': 'invalid_configuration', 'message': 'Assistant configuration is invalid. Check the assistant section in the active YAML file.'}
    return {'code': 'initialization_failed', 'message': 'Assistant initialization failed. Restart with the current code and configuration.'}


def provider_error(exc):
    status = getattr(exc, 'status', None)
    messages = {
        401: ('authentication_failed', 'NIM rejected the API key (HTTP 401). Replace the key and recreate the container.'),
        403: ('access_denied', 'NIM denied access (HTTP 403). Check API key permissions and model access.'),
        404: ('model_unavailable', 'NIM endpoint or model was not found (HTTP 404). Check the configured model and API base.'),
        429: ('rate_limited', 'NIM rate limit reached (HTTP 429). Requests will back off; try again later.'),
    }
    code, message = messages.get(status, ('provider_unavailable', 'NIM request failed. Check network connectivity, model availability and retry later.'))
    return {'code': code, 'message': message, 'http_status': status}
