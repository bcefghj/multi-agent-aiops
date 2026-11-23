"""告警检测链路与告警抑制的端到端测试。

`test_api.py` 覆盖了「单点指标不告警」，但没有覆盖这两条更关键的行为：

1. 样本积累到投票门槛后，突刺应当真的触发告警并走完四段链路；
2. `AlertFingerprint` 的 300s 去重窗口 —— 同一指标重复突刺只应告警一次。

两者都属于「静默失败」高风险区：链路断了、去重写错，接口仍然返回 200。
"""

import pytest
from fastapi.testclient import TestClient

from api.main import app

# IsolationForest 参与投票所需的样本数，低于该值时只有 3-Sigma 与 EWMA 两票
BASELINE_SAMPLES = 20


@pytest.fixture()
def incident_client():
    with TestClient(app) as client:
        yield client


def _post_metric(client, metric, value, service="order-service"):
    response = client.post(
        "/api/v1/incidents/trigger",
        json={"metric_name": metric, "metric_value": value, "target_service": service},
    )
    assert response.status_code == 200
    return response.json()


def _feed_baseline(client, metric, samples=BASELINE_SAMPLES, value=40.0):
    for _ in range(samples):
        _post_metric(client, metric, value)


def test_baseline_then_spike_triggers_full_workflow(incident_client):
    """基线积累到 20 个样本后，突刺应告警并跑完 monitor → rca → heal → change"""
    metric = "test_cpu_baseline_then_spike"
    _feed_baseline(incident_client, metric)

    event = _post_metric(incident_client, metric, 99.0)

    assert event["status"] == "resolved"
    assert event["alert"] is not None
    assert event["alert"]["metric"] == metric
    assert event["heal"]["action"] == "rollback"
    assert event["heal"]["level"] == "L1"
    assert event["change"]["status"] == "approved"

    statuses = {node["name"]: node["status"] for node in event["workflow"]}
    assert statuses == {
        "monitor": "completed",
        "rca": "completed",
        "heal": "completed",
        "change": "completed",
    }


def test_duplicate_alert_is_suppressed_within_window(incident_client):
    """同一「指标 + 服务」在指纹窗口内重复突刺，第二次不应再告警"""
    metric = "test_cpu_duplicate_spike"
    _feed_baseline(incident_client, metric)

    first = _post_metric(incident_client, metric, 99.0)
    second = _post_metric(incident_client, metric, 99.0)

    assert first["alert"] is not None
    assert second["alert"] is None
    assert second["status"] == "open"


@pytest.mark.xfail(
    strict=True,
    reason="BUG-001：metric_value 缺失时 None 写入指标历史，该指标后续检测永久失败",
)
def test_missing_metric_value_does_not_break_detection(incident_client):
    """缺参请求不应污染历史：修复后该用例应转为 pass（或改为断言 422）"""
    metric = "test_cpu_missing_value"
    incident_client.post(
        "/api/v1/incidents/trigger",
        json={"metric_name": metric, "target_service": "order-service"},
    )

    _feed_baseline(incident_client, metric)
    event = _post_metric(incident_client, metric, 99.0)

    assert event["alert"] is not None, "缺参请求污染了指标历史，后续告警被静默丢弃"
