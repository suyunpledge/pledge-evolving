"""Desktop compatibility entry point for the shared cancellable HTTP transport."""
from contextlib import contextmanager
from forge.http_transport import open_response as _open_response, RequestCancelled
from forge.secret_http import open_authenticated


@contextmanager
def open_response(request, *, timeout, cancel_event=None):
    # Keep the historical no-cancel injection seam for desktop integrations.
    if cancel_event is None:
        with open_authenticated(request, timeout=timeout) as response:
            yield response
    else:
        with _open_response(request, timeout=timeout, cancel_event=cancel_event) as response:
            yield response
