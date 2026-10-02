"""多车次同时占用查询测试。

求指定危险区在一段时间内被 >=threshold 辆互异车次同时占用的半开区间；
车次集合变化即分段，相邻同集合区间合并。沿用生效窗口、带洞边界、相邻点
线性插值与零时长不计规则。区域与所有车次点集必须来自同一已提交读视图。
"""
from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _batch(points):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]}


# 三车均以 2.5m/s 沿 y=50 横穿 0..100 方块，依次错开 10 秒出发：
# T1 区内 [10:00:20, 10:01:00)；T2 [10:00:30, 10:01:10)；T3 [10:00:40, 10:01:20)
T1_PTS = [
    ("a1", "2026-09-30T10:00:00Z", -50, 50),
    ("a2", "2026-09-30T10:01:20Z", 150, 50),
]
T2_PTS = [
    ("b1", "2026-09-30T10:00:10Z", -50, 50),
    ("b2", "2026-09-30T10:01:30Z", 150, 50),
]
T3_PTS = [
    ("c1", "2026-09-30T10:00:20Z", -50, 50),
    ("c2", "2026-09-30T10:01:40Z", 150, 50),
]

SINCE = "2026-09-30T10:00:00Z"
UNTIL = "2026-09-30T10:02:00Z"


def _setup_three(client, zone_id="co", exterior=None, holes=None, windows=None):
    assert register_square(
        client, zone_id=zone_id, exterior=exterior, holes=holes, windows=windows
    ).status_code == 201
    client.post("/api/v1/trips/T1/points", json=_batch(T1_PTS))
    client.post("/api/v1/trips/T2/points", json=_batch(T2_PTS))
    client.post("/api/v1/trips/T3/points", json=_batch(T3_PTS))


def _co(client, zone_id="co", trips=("T1", "T2", "T3"), threshold=2,
        since=SINCE, until=UNTIL, track_versions=None, revision=None):
    body = {
        "trip_ids": list(trips),
        "since": since,
        "until": until,
        "threshold": threshold,
    }
    if track_versions is not None:
        body["track_versions"] = track_versions
    url = f"/api/v1/zones/{zone_id}/co-occupancy"
    if revision is not None:
        url += f"?revision={revision}"
    return client.post(url, json=body)


# ---------------------------------------------------------------------
# 基础分段
# ---------------------------------------------------------------------

def test_threshold_two_segments_by_active_set(client):
    _setup_three(client)
    r = _co(client)
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["zone_id"] == "co"
    assert body["since"].endswith("Z") and body["until"].endswith("Z")
    assert body["threshold"] == 2
    assert body["trip_count"] == 3
    assert body["trip_ids"] == ["T1", "T2", "T3"]
    # 未指定版本时响应不出现版本回填字段
    assert "revision" not in body
    assert "track_versions" not in body

    segs = body["segments"]
    assert len(segs) == 3
    assert segs == [
        {
            "start": "2026-09-30T10:00:30Z",
            "end": "2026-09-30T10:00:40Z",
            "duration_sec": 10.0,
            "trip_count": 2,
            "trip_ids": ["T1", "T2"],
        },
        {
            "start": "2026-09-30T10:00:40Z",
            "end": "2026-09-30T10:01:00Z",
            "duration_sec": 20.0,
            "trip_count": 3,
            "trip_ids": ["T1", "T2", "T3"],
        },
        {
            "start": "2026-09-30T10:01:00Z",
            "end": "2026-09-30T10:01:10Z",
            "duration_sec": 10.0,
            "trip_count": 2,
            "trip_ids": ["T2", "T3"],
        },
    ]


def test_threshold_three_only_middle(client):
    _setup_three(client)
    r = _co(client, threshold=3)
    assert r.status_code == 200
    segs = r.json()["segments"]
    assert len(segs) == 1
    assert segs[0]["trip_ids"] == ["T1", "T2", "T3"]
    assert segs[0]["start"] == "2026-09-30T10:00:40Z"
    assert segs[0]["end"] == "2026-09-30T10:01:00Z"
    assert segs[0]["duration_sec"] == 20.0


def test_threshold_default_is_two(client):
    _setup_three(client)
    body = {"trip_ids": ["T1", "T2"], "since": SINCE, "until": UNTIL}
    r = client.post("/api/v1/zones/co/co-occupancy", json=body)
    assert r.status_code == 200
    # T1 ∩ T2 = [10:00:30, 10:01:00)
    segs = r.json()["segments"]
    assert len(segs) == 1
    assert segs[0]["trip_ids"] == ["T1", "T2"]
    assert segs[0]["duration_sec"] == 30.0


def test_query_window_clipping_halfopen(client):
    _setup_three(client)
    r = _co(client, since="2026-09-30T10:00:35Z", until="2026-09-30T10:00:55Z")
    segs = r.json()["segments"]
    assert segs == [
        {
            "start": "2026-09-30T10:00:35Z",
            "end": "2026-09-30T10:00:40Z",
            "duration_sec": 5.0,
            "trip_count": 2,
            "trip_ids": ["T1", "T2"],
        },
        {
            "start": "2026-09-30T10:00:40Z",
            "end": "2026-09-30T10:00:55Z",
            "duration_sec": 15.0,
            "trip_count": 3,
            "trip_ids": ["T1", "T2", "T3"],
        },
    ]


def test_below_threshold_returns_empty(client):
    _setup_three(client)
    # T1 上午穿越，T4 11 点才穿越：时间上不相交，合法但未达阈值 -> 空列表
    client.post(
        "/api/v1/trips/T4/points",
        json=_batch([
            ("d1", "2026-09-30T11:00:00Z", -50, 50),
            ("d2", "2026-09-30T11:01:20Z", 150, 50),
        ]),
    )
    r = _co(client, trips=("T1", "T4"))
    assert r.status_code == 200
    assert r.json()["segments"] == []


def test_hole_splits_co_occupancy(client):
    # 洞 x∈[30,70]：单车占用在洞前后各一段，共占只出现在两段重叠处，各 2 秒
    _setup_three(client, zone_id="hole-co", holes=[[[30, 30], [70, 30], [70, 70], [30, 70]]])
    r = _co(client, zone_id="hole-co", trips=("T1", "T2"))
    segs = r.json()["segments"]
    assert [(s["start"], s["end"], s["duration_sec"]) for s in segs] == [
        ("2026-09-30T10:00:30Z", "2026-09-30T10:00:32Z", 2.0),
        ("2026-09-30T10:00:58Z", "2026-09-30T10:01:00Z", 2.0),
    ]
    assert all(s["trip_ids"] == ["T1", "T2"] for s in segs)


def test_zero_duration_simultaneous_touch_not_counted(client):
    register_square(client, zone_id="touch-co")
    # 两车轨迹都仅在同一瞬间擦过角点 (0,0)（10:00:05），零时长不计
    client.post(
        "/api/v1/trips/K1/points",
        json=_batch([
            ("k1", "2026-09-30T10:00:00Z", -10, 10),
            ("k2", "2026-09-30T10:00:10Z", 10, -10),
        ]),
    )
    client.post(
        "/api/v1/trips/K2/points",
        json=_batch([
            ("k3", "2026-09-30T10:00:00Z", -10, -10),
            ("k4", "2026-09-30T10:00:10Z", 0, 0),
        ]),
    )
    r = _co(client, zone_id="touch-co", trips=("K1", "K2"))
    assert r.status_code == 200, r.text
    assert r.json()["segments"] == []


def test_zone_window_halfopen_boundary(client):
    # 区域仅在 [10:00:30, 10:01:00) 生效：T1∩T2∩窗口
    register_square(
        client,
        zone_id="win-co",
        windows=[{"start": "2026-09-30T10:00:30Z", "end": "2026-09-30T10:01:00Z"}],
    )
    client.post("/api/v1/trips/T1/points", json=_batch(T1_PTS))
    client.post("/api/v1/trips/T2/points", json=_batch(T2_PTS))
    r = _co(client, zone_id="win-co", trips=("T1", "T2"))
    segs = r.json()["segments"]
    assert len(segs) == 1
    assert segs[0]["start"] == "2026-09-30T10:00:30Z"
    assert segs[0]["end"] == "2026-09-30T10:01:00Z"
    assert segs[0]["trip_ids"] == ["T1", "T2"]


# ---------------------------------------------------------------------
# 错误处理
# ---------------------------------------------------------------------

def test_unknown_zone_404(client):
    _setup_three(client)
    r = _co(client, zone_id="nope")
    assert r.status_code == 404
    assert r.json()["error"] == "not_found"


def test_unknown_trip_404(client):
    _setup_three(client)
    r = _co(client, trips=("T1", "ghost"))
    assert r.status_code == 404
    assert "ghost" in r.json()["detail"]


def test_duplicate_trip_ids_rejected(client):
    _setup_three(client)
    r = _co(client, trips=("T1", "T1"))
    assert r.status_code == 400
    assert "互异" in r.json()["detail"] or "重复" in r.json()["detail"]


def test_blank_trip_id_rejected(client):
    _setup_three(client)
    r = _co(client, trips=("T1", "   "))
    assert r.status_code == 400


def test_threshold_out_of_range_rejected(client):
    _setup_three(client)
    # > 车次数：动态上界，服务层 400
    r = _co(client, threshold=9)
    assert r.status_code == 400
    assert "threshold" in r.json()["detail"]


def test_threshold_below_two_rejected(client):
    _setup_three(client)
    r = _co(client, threshold=1)
    assert r.status_code == 422


def test_naive_time_rejected(client):
    _setup_three(client)
    r = _co(client, since="2026-09-30T10:00:00", until=UNTIL)
    assert r.status_code == 400
    assert "时区" in r.json()["detail"]


def test_empty_or_reversed_range_rejected(client):
    _setup_three(client)
    r = _co(client, since=UNTIL, until=SINCE)
    assert r.status_code == 400
    r2 = _co(client, since=SINCE, until=SINCE)
    assert r2.status_code == 400


def test_track_versions_unknown_key_rejected(client):
    _setup_three(client)
    r = _co(client, trips=("T1", "T2"), track_versions={"ghost": 1})
    assert r.status_code == 400


def test_track_version_negative_rejected(client):
    _setup_three(client)
    r = _co(client, trips=("T1", "T2"), track_versions={"T1": -1})
    assert r.status_code == 400


def test_track_version_missing_404(client):
    _setup_three(client)
    # 首批上传后最早可追溯版本为 1；0 与 9 都不存在
    r = _co(client, trips=("T1", "T2"), track_versions={"T1": 0})
    assert r.status_code == 404
    assert "基线" in r.json()["detail"]
    r = _co(client, trips=("T1", "T2"), track_versions={"T2": 9})
    assert r.status_code == 404
    assert "晚于" in r.json()["detail"]


def test_revision_out_of_range_404(client):
    _setup_three(client)
    r = _co(client, revision=999)
    assert r.status_code == 404


def test_revision_zone_not_yet_registered_404(client):
    # 修订号 1 登记的是 other，co 尚未登记
    register_square(client, zone_id="other")
    register_square(client, zone_id="co")
    client.post("/api/v1/trips/T1/points", json=_batch(T1_PTS))
    client.post("/api/v1/trips/T2/points", json=_batch(T2_PTS))
    r = _co(client, revision=1)
    assert r.status_code == 404
    assert "尚未登记" in r.json()["detail"]


def test_extra_field_rejected(client):
    _setup_three(client)
    body = {"trip_ids": ["T1", "T2"], "since": SINCE, "until": UNTIL, "bogus": 1}
    r = client.post("/api/v1/zones/co/co-occupancy", json=body)
    assert r.status_code == 422


def test_single_trip_rejected(client):
    _setup_three(client)
    body = {"trip_ids": ["T1"], "since": SINCE, "until": UNTIL}
    r = client.post("/api/v1/zones/co/co-occupancy", json=body)
    assert r.status_code == 422


# ---------------------------------------------------------------------
# 版本语义
# ---------------------------------------------------------------------

def test_pinned_track_version_reproduces_overlap(client):
    register_square(client, zone_id="co")
    client.post("/api/v1/trips/T1/points", json=_batch(T1_PTS))
    client.post("/api/v1/trips/T2/points", json=_batch(T2_PTS))
    # 当前共占 30 秒
    assert len(_co(client, trips=("T1", "T2")).json()["segments"]) == 1

    # 更正 T2：整条轨迹平移到 11 点，当前不再与 T1 共占
    r = client.post(
        "/api/v1/trips/T2/corrections",
        json={
            "base_version": 1,
            "reason": "设备时间错了一个小时",
            "items": [
                {"action": "update", "point_id": "b1",
                 "ts": "2026-09-30T11:00:10Z", "x": -50, "y": 50},
                {"action": "update", "point_id": "b2",
                 "ts": "2026-09-30T11:01:30Z", "x": 150, "y": 50},
            ],
        },
    )
    assert r.status_code == 200, r.text
    assert _co(client, trips=("T1", "T2")).json()["segments"] == []

    # 钉住 T2 版本 1：当时的共占可复现；只回填被钉住的车次
    r = _co(client, trips=("T1", "T2"), track_versions={"T2": 1})
    body = r.json()
    assert len(body["segments"]) == 1
    assert body["segments"][0]["trip_ids"] == ["T1", "T2"]
    assert body["segments"][0]["duration_sec"] == 30.0
    assert body["track_versions"] == {"T2": 1}
    assert "revision" not in body


def test_pinned_revision_and_track_version_echo_together(client):
    r = register_square(client, zone_id="co")
    assert r.status_code == 201
    client.post("/api/v1/trips/T1/points", json=_batch(T1_PTS))
    client.post("/api/v1/trips/T2/points", json=_batch(T2_PTS))

    # 修订号 2：把区域改成远处的小方块，最新配置下无共占
    client.post(
        "/api/v1/zones",
        json={
            "zone_id": "co",
            "exterior": [[500, 500], [520, 500], [520, 520], [500, 520]],
            "holes": [],
            "windows": DAY_WINDOWS,
        },
    )
    assert _co(client, trips=("T1", "T2")).json()["segments"] == []

    r = _co(
        client,
        trips=("T1", "T2"),
        track_versions={"T1": 1, "T2": 1},
        revision=1,
    )
    body = r.json()
    assert body["revision"] == 1
    assert body["track_versions"] == {"T1": 1, "T2": 1}
    assert len(body["segments"]) == 1
    assert body["segments"][0]["duration_sec"] == 30.0


def test_revoked_all_trip_still_exists_empty_result(client):
    register_square(client, zone_id="co")
    client.post("/api/v1/trips/T1/points", json=_batch(T1_PTS))
    client.post("/api/v1/trips/T2/points", json=_batch(T2_PTS))
    client.post(
        "/api/v1/trips/T2/corrections",
        json={
            "base_version": 1,
            "reason": "整车数据作废",
            "items": [
                {"action": "revoke", "point_id": "b1"},
                {"action": "revoke", "point_id": "b2"},
            ],
        },
    )
    # T2 仍存在（版本流水还在），只是最新点集为空 -> 合法空结果，不是 404
    r = _co(client, trips=("T1", "T2"))
    assert r.status_code == 200
    assert r.json()["segments"] == []


# ---------------------------------------------------------------------
# 扫线合并规则（直接构造占用区间做单元验证）
# ---------------------------------------------------------------------

def _helper(client):
    from app.main import service
    return service


def test_helper_adjacent_same_set_merges(client):
    svc = _helper(client)
    # B 在 t=10 瞬时离开又回来（其自身区间端点相接），共占集合恒为 {A,B} -> 合并
    occ = {"A": [(0.0, 20.0)], "B": [(0.0, 10.0), (10.0, 20.0)]}
    segs = svc._co_occupancy_segments(occ, ["A", "B"], 2)
    assert segs == [(0.0, 20.0, ("A", "B"))]


def test_helper_gap_same_set_not_merged(client):
    svc = _helper(client)
    # 中间隔着 A 独占的真间隙 -> 不合并
    occ = {"A": [(0.0, 30.0)], "B": [(0.0, 8.0), (12.0, 20.0)]}
    segs = svc._co_occupancy_segments(occ, ["A", "B"], 2)
    assert segs == [(0.0, 8.0, ("A", "B")), (12.0, 20.0, ("A", "B"))]


def test_helper_set_change_splits(client):
    svc = _helper(client)
    # AB -> AC -> AB：集合变化即分段，首尾同集也不跨段合并
    occ = {
        "A": [(0.0, 30.0)],
        "B": [(0.0, 10.0), (20.0, 30.0)],
        "C": [(10.0, 20.0)],
    }
    segs = svc._co_occupancy_segments(occ, ["A", "B", "C"], 2)
    assert segs == [
        (0.0, 10.0, ("A", "B")),
        (10.0, 20.0, ("A", "C")),
        (20.0, 30.0, ("A", "B")),
    ]


def test_helper_point_overlap_is_empty(client):
    svc = _helper(client)
    occ = {"A": [(0.0, 10.0)], "B": [(10.0, 20.0)]}
    assert svc._co_occupancy_segments(occ, ["A", "B"], 2) == []


def test_helper_trip_order_follows_request(client):
    svc = _helper(client)
    occ = {"A": [(0.0, 10.0)], "B": [(0.0, 10.0)], "C": [(0.0, 10.0)]}
    segs = svc._co_occupancy_segments(occ, ["C", "A", "B"], 2)
    assert segs == [(0.0, 10.0, ("C", "A", "B"))]
