"""DSH integration may embed the UI without enabling cross-site API calls."""
import json
import threading
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest

from advisor.server import App, make_server


@pytest.mark.parametrize('enabled', [False, True])
def test_only_explicit_dsh_launch_allows_local_frame_parents(tmp_path, monkeypatch, enabled):
    if enabled:
        monkeypatch.setenv('SIM_ADVISOR_DSH_EMBED', '1')
    else:
        monkeypatch.delenv('SIM_ADVISOR_DSH_EMBED', raising=False)
    app = App(tmp_path / 'data', doc_root=tmp_path, runtime_factory='none')
    server = make_server(app, 0)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    base = f'http://127.0.0.1:{server.server_port}'
    try:
        with urlopen(base, timeout=5) as response:
            policy = response.headers['Content-Security-Policy']
        if enabled:
            assert 'frame-ancestors dsh-app://app http://127.0.0.1:* http://localhost:*;' in policy
        else:
            assert "frame-ancestors 'none';" in policy
        assert "connect-src 'self';" in policy and "script-src 'self';" in policy
        request = Request(base + '/api/sessions', data=json.dumps({'goal': 'Do not accept untrusted parent API requests'}).encode(),
                          headers={'Content-Type': 'application/json', 'Origin': 'https://untrusted.example'})
        with pytest.raises(HTTPError) as caught:
            urlopen(request, timeout=5)
        assert caught.value.code == 403
    finally:
        server.shutdown()
        server.server_close()
        app.agents.close()
        thread.join(timeout=3)
