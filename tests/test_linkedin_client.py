import json

import httpx
import pytest

from app.linkedin import (
    AuthLookupError,
    ImageUploadError,
    LinkedInClient,
    PostMaybeLive,
    PostRejected,
    build_post_payload,
)

PAYLOAD = build_post_payload("urn:li:person:x", "Hi #AI (test)", "urn:li:image:1", "Hi")


def client(handler) -> LinkedInClient:
    http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
    return LinkedInClient(http, "secret-token", "202601")


def raiser(exc_type):
    def handler(request):
        raise exc_type("boom", request=request)

    return handler


async def test_create_post_success_sends_required_headers():
    seen = {}

    def handler(request: httpx.Request):
        seen["headers"] = request.headers
        seen["body"] = json.loads(request.content)
        seen["url"] = str(request.url)
        return httpx.Response(201, headers={"x-restli-id": "urn:li:share:9"})

    assert await client(handler).create_post(PAYLOAD) == "urn:li:share:9"
    h = seen["headers"]
    assert h["LinkedIn-Version"] == "202601" and h["X-Restli-Protocol-Version"] == "2.0.0"
    assert h["Authorization"] == "Bearer secret-token"
    assert seen["url"] == "https://api.linkedin.com/rest/posts"
    assert seen["body"]["commentary"] == "Hi {hashtag|\\#|AI} \\(test\\)"


@pytest.mark.parametrize("status", [400, 401, 403, 422, 429])
async def test_4xx_is_rejected_retryable(status):
    with pytest.raises(PostRejected, match=f"HTTP {status}"):
        await client(lambda r: httpx.Response(status, text="nope")).create_post(PAYLOAD)


async def test_401_hints_reauth():
    with pytest.raises(PostRejected, match="linkedin_auth.py"):
        await client(lambda r: httpx.Response(401)).create_post(PAYLOAD)


@pytest.mark.parametrize("status", [500, 502, 503, 504])
async def test_5xx_maybe_live(status):
    with pytest.raises(PostMaybeLive):
        await client(lambda r: httpx.Response(status)).create_post(PAYLOAD)


@pytest.mark.parametrize("exc", [httpx.ReadTimeout, httpx.WriteTimeout, httpx.ReadError, httpx.RemoteProtocolError])
async def test_timeouts_and_broken_responses_maybe_live(exc):
    with pytest.raises(PostMaybeLive):
        await client(raiser(exc)).create_post(PAYLOAD)


@pytest.mark.parametrize("exc", [httpx.ConnectError, httpx.ConnectTimeout, httpx.PoolTimeout])
async def test_never_sent_is_retryable(exc):
    with pytest.raises(PostRejected, match="never reached"):
        await client(raiser(exc)).create_post(PAYLOAD)


async def test_2xx_without_restli_id_maybe_live():
    with pytest.raises(PostMaybeLive):
        await client(lambda r: httpx.Response(201)).create_post(PAYLOAD)


async def test_init_upload_parses_response():
    def handler(request):
        assert request.url.params["action"] == "initializeUpload"
        assert json.loads(request.content) == {"initializeUploadRequest": {"owner": "urn:li:person:x"}}
        assert request.headers["LinkedIn-Version"] == "202601"
        return httpx.Response(200, json={"value": {"uploadUrl": "https://up/1", "image": "urn:li:image:5"}})

    assert await client(handler).init_upload("urn:li:person:x") == ("https://up/1", "urn:li:image:5")


@pytest.mark.parametrize(
    "handler",
    [lambda r: httpx.Response(403), lambda r: httpx.Response(500), lambda r: httpx.Response(200, json={}), raiser(httpx.ReadTimeout)],
)
async def test_init_upload_failures_are_upload_errors(handler):
    with pytest.raises(ImageUploadError):
        await client(handler).init_upload("urn:li:person:x")


async def test_put_image_sends_bytes_and_type():
    seen = {}

    def handler(request):
        seen.update(method=request.method, ctype=request.headers["Content-Type"], n=len(request.content))
        return httpx.Response(201)

    await client(handler).put_image("https://up/1", b"\x89PNG....", "image/png")
    assert seen == {"method": "PUT", "ctype": "image/png", "n": 8}


@pytest.mark.parametrize("handler", [lambda r: httpx.Response(500), raiser(httpx.ReadTimeout), raiser(httpx.ConnectError)])
async def test_put_failures_are_upload_errors(handler):
    with pytest.raises(ImageUploadError):
        await client(handler).put_image("https://up/1", b"x", "image/png")


async def test_userinfo():
    assert await client(lambda r: httpx.Response(200, json={"sub": "abc"})).userinfo_sub() == "abc"
    with pytest.raises(AuthLookupError):
        await client(lambda r: httpx.Response(401)).userinfo_sub()
    with pytest.raises(AuthLookupError):
        await client(lambda r: httpx.Response(200, text="not json")).userinfo_sub()


def test_payload_shape():
    assert PAYLOAD["distribution"]["feedDistribution"] == "MAIN_FEED"
    assert PAYLOAD["content"] == {"media": {"id": "urn:li:image:1", "title": "Hi"}}
