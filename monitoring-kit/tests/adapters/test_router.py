from monitoring_kit.adapters.upstream.router import GatewayRouter
from monitoring_kit.collection.ports import UpstreamError

from tests.support import ScriptedGateway, request


def test_gateway_router_falls_back_only_on_explicitly_allowed_failure():
    primary = ScriptedGateway([], submit_failures=[UpstreamError("RATE_LIMITED", "限流", fallback_allowed=True)], gateway_key="primary")
    secondary = ScriptedGateway([], gateway_key="secondary")
    router = GatewayRouter([primary, secondary])
    from monitoring_kit.collection.model import UpstreamJobRequest

    upstream_request = UpstreamJobRequest(request().collection)
    ref = router.submit(upstream_request, "idem-1")
    assert ref.gateway_key == secondary.gateway_key
    assert primary.submit_calls == 1
    assert secondary.submit_calls == 1
    assert router.submit(upstream_request, "idem-1") == ref
    assert secondary.submit_calls == 1


def test_gateway_router_does_not_switch_after_ambiguous_submission():
    primary = ScriptedGateway(
        [],
        submit_failures=[
            UpstreamError(
                "NETWORK",
                "响应不确定",
                retryable=True,
                fallback_allowed=True,
                submission_unknown=True,
            )
        ],
    )
    secondary = ScriptedGateway([], gateway_key="secondary")
    router = GatewayRouter([primary, secondary])
    from monitoring_kit.collection.model import UpstreamJobRequest

    try:
        router.submit(UpstreamJobRequest(request().collection), "idem-2")
    except UpstreamError as error:
        assert error.submission_unknown is True
    else:  # pragma: no cover
        raise AssertionError("应该保留不确定提交错误")
    assert secondary.submit_calls == 0
