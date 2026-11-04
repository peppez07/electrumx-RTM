import pytest
from aiohttp.test_utils import TestClient, TestServer

from electrumx.explorer.service import create_app
from tests.explorer.test_api import Core, Electrum


@pytest.mark.asyncio
async def test_ui_and_assets_served_with_same_origin_security():
    async with TestClient(TestServer(create_app(Core(), Electrum()))) as client:
        response = await client.get('/')
        assert response.status == 200
        page = await response.text()
        assert 'Raptoreum' in page
        # Donation address and the MetaYoshi gift button (wired up in app.js; the CSP allows no inline script).
        assert 'RM2yPaWZYmDo5HfqAtfYsh1jhSi9rVncuu' in page
        assert 'id="gift-yoshi"' in page and 'data-amount="1000"' in page and 'Buy Yoshi a gift!' in page
        assert 'RVLFQj4RiMCDDrWGZXRd2ui1NKGQ9ieooz' not in page and 'onclick' not in page
        for name, content_type in [('app.js', 'javascript'), ('style.css', 'text/css')]:
            response = await client.get('/static/'+name)
            assert response.status == 200
            assert content_type in response.headers['Content-Type']
            assert "script-src 'self'" in response.headers['Content-Security-Policy']
            assert 'img-src \'self\' data: https://ipfsm.raptoreum.com' in response.headers['Content-Security-Policy']
        assert (await client.get('/static/../clients.py')).status == 404


@pytest.mark.asyncio
async def test_api_errors_are_json_and_do_not_expose_backend_messages():
    core = Core()
    async def broken(*args):
        raise RuntimeError('http://root:PRIVATE_PASSWORD@localhost:10225')
    core.call = broken
    async with TestClient(TestServer(create_app(core, Electrum()))) as client:
        response = await client.get('/api/v1/status')
        assert response.status == 502
        assert 'PRIVATE' not in await response.text()
