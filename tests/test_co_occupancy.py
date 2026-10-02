"""多车同时占用审计测试。

POST /api/v1/zones/{zone_id}/co-occupancy：查明指定危险区在一段时间内
被多辆作业车同时占用的时段。区域与全部车次点集从同一已提交读视图读取；
返回达到阈值的半开区间及区间内恒定的车次集合，集合变化即分段，
相邻同集合区间合并；合法但未达阈值返回空列表。
"""
import threading

from tests.conftest import DAY_WINDOWS, SQUARE, register_square


def _batch(points):
    return {"points": [{"point_id": pid, "ts": ts, "x": x, "y": y} for pid, ts, x, y in points]}


def _upload(client, trip, points):
    r = client.post(f"/api/v1/trips/{trip}/points", json=_batch(points))
    assert r.status_code == 200, r.text
    return r


def _ask(client, zone_id, **overrides):
    body = {
        "trip_ids": ["T1", "T2"],
        "since": "2026-09-30T10:00:00Z",
        "until": "2026-09-30T10:10:00Z",
        "threshold": 2,
    }
    body.update(overrides)
    return client.post(f"/api/v1/zones/{zone_id}/co-occupancy", json=body)


# 两车均以 2.5m/s 横穿方块 [0,100]x[0,100]：占用 [10:00:20, 10:01:00)
CROSS_X = [
    ("a", "2026-09-30T10:00:00Z", -50, 50),
    ("b", "2026-09-30T10:01:20Z", 150, 50),
]
CROSS_Y = [
    ("a", "2026-09-30T10:00:00Z", 50, -50),
    ("b", "2026-09-30T10:01:20Z", 50, 150),
]


def _still(trip_prefix, x, y, t0, t1):
    return [
        (f"{trip_prefix}1", t0, x, y),
        (f"{trip_prefix}2", t1, x, y),
    ]


# ---------------------------------------------------------------------
# 基本语义
# ---------------------------------------------------------------------

def test_basic_two_trip_overlap(client):
    register_square(client, zone_id="co1")
    _upload(client, "T1", CROSS_X)
    _upload(client, "T2", CROSS_Y)

    r = _ask(client, "co1", since="2026-09-30T10:00:00Z", until="2026-09-30T10:02:00Z")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["zone_id"] == "co1"
    assert body["threshold"] == 2
    assert body["since"] == "2026-09-30T10:00:00Z"
    assert body["until"] == "2026-09-30T10:02:00Z"
    assert "revision" not in body  # 未指定修订号：不出现
    # 响应回填每车实际使用的轨迹版本与点数
    trips = {t["trip_id"]: t for t in body["trips"]}
    assert trips["T1"]["track_version"] == 1 and trips["T1"]["point_count"] == 2
    assert trips["T2"]["track_version"] == 1 and trips["T2"]["point_count"] == 2

    assert len(body["intervals"]) == 1
    seg = body["intervals"][0]
    assert seg["start"] == "2026-09-30T10:00:20Z"
    assert seg["end"] == "2026-09-30T10:01:00Z"
    assert seg["duration_sec"] == 40.0
    assert seg["trip_ids"] == ["T1", "T2"]
    assert seg["count"] == 2


def test_trip_set_change_splits_intervals(client):
    """车次集合变化即分段：三车错峰静止在区内。"""
    register_square(client, zone_id="co2")
    _upload(client, "T1", _still("a", 50, 50, "2026-09-30T10:00:00Z", "2026-09-30T10:10:00Z"))
    _upload(client, "T2", _still("b", 60, 60, "2026-09-30T10:02:00Z", "2026-09-30T10:04:00Z"))
    _upload(client, "T3", _still("c", 70, 70, "2026-09-30T10:03:00Z", "2026-09-30T10:05:00Z"))

    r = _ask(client, "co2", trip_ids=["T1", "T2", "T3"], threshold=2)
    assert r.status_code == 200, r.text
    intervals = r.json()["intervals"]
    assert [(i["start"], i["end"], i["trip_ids"], i["count"]) for i in intervals] == [
        ("2026-09-30T10:02:00Z", "2026-09-30T10:03:00Z", ["T1", "T2"], 2),
        ("2026-09-30T10:03:00Z", "2026-09-30T10:04:00Z", ["T1", "T2", "T3"], 3),
        ("2026-09-30T10:04:00Z", "2026-09-30T10:05:00Z", ["T1", "T3"], 2),
    ]

    # 阈值 = 车次数：仅三者共同占用段
    r = _ask(client, "co2", trip_ids=["T1", "T2", "T3"], threshold=3)
    intervals = r.json()["intervals"]
    assert len(intervals) == 1
    assert intervals[0]["start"] == "2026-09-30T10:03:00Z"
    assert intervals[0]["end"] == "2026-09-30T10:04:00Z"
    assert intervals[0]["trip_ids"] == ["T1", "T2", "T3"]


def test_below_threshold_returns_empty_list(client):
    """合法但未达阈值：返回空列表（HTTP 200）。"""
    register_square(client, zone_id="co3")
    _upload(client, "T1", CROSS_X)
    # T2 在 T1 离开后才进入
    _upload(client, "T2", [
        ("a", "2026-09-30T10:02:00Z", 50, -50),
        ("b", "2026-09-30T10:03:20Z", 50, 150),
    ])
    r = _ask(client, "co3", since="2026-09-30T10:00:00Z", until="2026-09-30T10:05:00Z")
    assert r.status_code == 200
    assert r.json()["intervals"] == []

    # 三车阈值 3，但任何时刻最多两车同时在区内
    _upload(client, "T3", CROSS_Y)
    r = _ask(
        client, "co3",
        trip_ids=["T1", "T2", "T3"], threshold=3,
        since="2026-09-30T10:00:00Z", until="2026-09-30T10:05:00Z",
    )
    assert r.status_code == 200
    assert r.json()["intervals"] == []


def test_query_range_clips_occupancy(client):
    """占用按半开 [since, until) 裁剪；零时长接触不计。"""
    register_square(client, zone_id="co4")
    _upload(client, "T1", CROSS_X)
    _upload(client, "T2", CROSS_Y)
    r = _ask(client, "co4", since="2026-09-30T10:00:30Z", until="2026-09-30T10:00:50Z")
    seg = r.json()["intervals"][0]
    assert seg["start"] == "2026-09-30T10:00:30Z"
    assert seg["end"] == "2026-09-30T10:00:50Z"
    assert seg["duration_sec"] == 20.0

    # 查询范围恰好只碰到占用端点（零时长）：不计
    r = _ask(client, "co4", since="2026-09-30T10:01:00Z", until="2026-09-30T10:02:00Z")
    assert r.json()["intervals"] == []


def test_zone_window_and_hole_apply(client):
    """沿用区域生效窗口与带洞边界规则：窗口外与洞内时间不计。"""
    hole = [[[40, 40], [60, 40], [60, 60], [40, 60]]]
    register_square(
        client, zone_id="co5", holes=hole,
        windows=[{"start": "2026-09-30T10:00:25Z", "end": "2026-09-30T10:00:55Z"}],
    )
    # 两车同时横穿 y=50：x=0 在 10:00:20，洞 [40,60] 对应 10:00:36~10:00:44，x=100 在 10:01:00
    _upload(client, "T1", CROSS_X)
    _upload(client, "T2", CROSS_X)
    r = _ask(client, "co5", since="2026-09-30T10:00:00Z", until="2026-09-30T10:02:00Z")
    intervals = r.json()["intervals"]
    # 窗口 [10:00:25, 10:00:55) ∩ 区内（去洞）→ 两段
    assert [(i["start"], i["end"]) for i in intervals] == [
        ("2026-09-30T10:00:25Z", "2026-09-30T10:00:36Z"),
        ("2026-09-30T10:00:44Z", "2026-09-30T10:00:55Z"),
    ]
    assert all(i["trip_ids"] == ["T1", "T2"] for i in intervals)


def test_zero_duration_contact_not_counted(client):
    register_square(client, zone_id="co6")
    # 两车同时仅擦过角点 (0,0)
    _upload(client, "T1", [
        ("a", "2026-09-30T10:00:00Z", -10, 10),
        ("b", "2026-09-30T10:00:10Z", 10, -10),
    ])
    _upload(client, "T2", [
        ("a", "2026-09-30T10:00:00Z", -10, 10),
        ("b", "2026-09-30T10:00:10Z", 10, -10),
    ])
    r = _ask(client, "co6", since="2026-09-30T10:00:00Z", until="2026-09-30T10:01:00Z")
    assert r.status_code == 200
    assert r.json()["intervals"] == []


def test_trip_with_single_point_never_occupies(client):
    """点数不足 2 的车次不产生占用，但请求本身合法。"""
    register_square(client, zone_id="co7")
    _upload(client, "T1", CROSS_X)
    _upload(client, "T2", [("solo", "2026-09-30T10:00:30Z", 50, 50)])
    r = _ask(client, "co7", since="2026-09-30T10:00:00Z", until="2026-09-30T10:02:00Z")
    assert r.status_code == 200
    assert r.json()["intervals"] == []
    trips = {t["trip_id"]: t for t in r.json()["trips"]}
    assert trips["T2"]["point_count"] == 1


def test_latest_committed_state_used_when_version_omitted(client):
    """未指定版本的车取查询时最新已提交状态：新点上传后立即生效。"""
    register_square(client, zone_id="co8")
    _upload(client, "T1", CROSS_X)
    _upload(client, "T2", [("a", "2026-09-30T10:00:00Z", 50, -50)])
    r = _ask(client, "co8", since="2026-09-30T10:00:00Z", until="2026-09-30T10:02:00Z")
    assert r.json()["intervals"] == []  # T2 仅一点，无占用

    # 追加第二点（真正新点 -> 版本 2）：T2 开始占用
    _upload(client, "T2", [("b", "2026-09-30T10:01:20Z", 50, 150)])
    r = _ask(client, "co8", since="2026-09-30T10:00:00Z", until="2026-09-30T10:02:00Z")
    assert len(r.json()["intervals"]) == 1
    trips = {t["trip_id"]: t for t in r.json()["trips"]}
    assert trips["T2"]["track_version"] == 2


# ---------------------------------------------------------------------
# 逐车轨迹版本与区域修订号
# ---------------------------------------------------------------------

def test_pinned_track_versions_reproduce(client):
    """逐车轨迹版本：按指定版本点集核算；未指定的车取当前点集。"""
    register_square(client, zone_id="co9")
    _upload(client, "T1", CROSS_X)
    _upload(client, "T2", CROSS_Y)
    # 更正 T1：b 点拉到区外 -> 版本 2 不再穿越
    r = client.post(
        "/api/v1/trips/T1/corrections",
        json={
            "base_version": 1,
            "reason": "b 点坐标录错",
            "items": [{"action": "update", "point_id": "b",
                       "ts": "2026-09-30T10:01:20Z", "x": 150, "y": 500}],
        },
    )
    assert r.status_code == 200

    # 不指定版本：T1 当前点集不穿越 -> 空
    r = _ask(client, "co9", since="2026-09-30T10:00:00Z", until="2026-09-30T10:02:00Z")
    assert r.json()["intervals"] == []

    # 指定 T1@1：按旧点集复现 -> 与 T2 重叠 40s
    r = _ask(
        client, "co9",
        since="2026-09-30T10:00:00Z", until="2026-09-30T10:02:00Z",
        track_versions={"T1": 1},
    )
    body = r.json()
    assert len(body["intervals"]) == 1
    assert body["intervals"][0]["duration_sec"] == 40.0
    trips = {t["trip_id"]: t for t in body["trips"]}
    assert trips["T1"]["track_version"] == 1   # 指定的版本回填
    assert trips["T2"]["track_version"] == 1   # 未指定：当前版本回填


def test_pinned_revision_reproduces_zone_config(client):
    """区域修订号：按截至该号的区域配置核算，响应回填 revision。"""
    hole = [[[40, 40], [60, 40], [60, 60], [40, 60]]]
    r = register_square(client, zone_id="co10", holes=hole)   # rev1 带洞
    assert r.status_code == 201
    register_square(client, zone_id="co10")                   # rev2 无洞
    _upload(client, "T1", CROSS_X)
    _upload(client, "T2", CROSS_X)

    # 最新（无洞）：一段 40s
    r = _ask(client, "co10", since="2026-09-30T10:00:00Z", until="2026-09-30T10:02:00Z")
    body = r.json()
    assert "revision" not in body
    assert len(body["intervals"]) == 1
    assert body["intervals"][0]["duration_sec"] == 40.0

    # revision=1（带洞）：洞内 8s 剔除，两段
    r = _ask(
        client, "co10",
        since="2026-09-30T10:00:00Z", until="2026-09-30T10:02:00Z",
        revision=1,
    )
    body = r.json()
    assert body["revision"] == 1
    assert [(i["start"], i["end"]) for i in body["intervals"]] == [
        ("2026-09-30T10:00:20Z", "2026-09-30T10:00:36Z"),
        ("2026-09-30T10:00:44Z", "2026-09-30T10:01:00Z"),
    ]


def test_revision_and_track_versions_combine(client):
    """区域修订号与逐车轨迹版本可同时指定。"""
    register_square(client, zone_id="co11")                   # rev1: 0..100
    register_square(client, zone_id="co11", exterior=[[0, 0], [200, 0], [200, 200], [0, 200]])  # rev2
    _upload(client, "T1", CROSS_X)
    _upload(client, "T2", CROSS_Y)

    r = _ask(
        client, "co11",
        since="2026-09-30T10:00:00Z", until="2026-09-30T10:02:00Z",
        revision=1, track_versions={"T1": 1, "T2": 1},
    )
    body = r.json()
    assert body["revision"] == 1
    assert len(body["intervals"]) == 1
    assert body["intervals"][0]["duration_sec"] == 40.0


# ---------------------------------------------------------------------
# 错误：区域 / 车次 / 版本不存在
# ---------------------------------------------------------------------

def test_zone_not_found(client):
    _upload(client, "T1", CROSS_X)
    _upload(client, "T2", CROSS_Y)
    r = _ask(client, "ghost-zone")
    assert r.status_code == 404
    assert "危险区不存在" in r.json()["detail"]


def test_zone_not_registered_at_revision(client):
    register_square(client, zone_id="co-z")
    _upload(client, "T1", CROSS_X)
    _upload(client, "T2", CROSS_Y)
    # 修订号超出当前范围
    r = _ask(client, "co-z", revision=999)
    assert r.status_code == 404
    assert "超出当前范围" in r.json()["detail"]
    # 修订号存在但该配置中此区尚未登记
    register_square(client, zone_id="other-zone")  # rev2
    r = _ask(client, "other-zone", revision=1)
    assert r.status_code == 404
    assert "尚未登记" in r.json()["detail"]


def test_trip_not_found(client):
    register_square(client, zone_id="co-t")
    _upload(client, "T1", CROSS_X)
    # 未指定版本：车次不存在
    r = _ask(client, "co-t", trip_ids=["T1", "ghost"])
    assert r.status_code == 404
    assert "车次不存在" in r.json()["detail"]
    # 指定版本：车次不存在（无任何已保留的轨迹版本）
    r = _ask(client, "co-t", trip_ids=["T1", "ghost"], track_versions={"ghost": 1})
    assert r.status_code == 404
    assert "车次不存在" in r.json()["detail"]
    # 指定版本晚于当前版本
    _upload(client, "T2", CROSS_Y)
    r = _ask(client, "co-t", trip_ids=["T1", "T2"], track_versions={"T2": 2})
    assert r.status_code == 404
    assert "晚于" in r.json()["detail"]


def test_track_version_out_of_range(client):
    register_square(client, zone_id="co-v")
    _upload(client, "T1", CROSS_X)
    _upload(client, "T2", CROSS_Y)
    # 早于基线（新车次基线为 1）
    r = _ask(client, "co-v", track_versions={"T1": 0})
    assert r.status_code == 404
    assert "基线" in r.json()["detail"]
    # 晚于当前
    r = _ask(client, "co-v", track_versions={"T2": 9})
    assert r.status_code == 404
    assert "晚于" in r.json()["detail"]


# ---------------------------------------------------------------------
# 错误：请求非法（时间范围 / 重复车次 / 阈值 / 版本格式）
# ---------------------------------------------------------------------

def test_duplicate_trip_ids_rejected(client):
    register_square(client, zone_id="co-d")
    _upload(client, "T1", CROSS_X)
    r = _ask(client, "co-d", trip_ids=["T1", "T1"])
    assert r.status_code == 400
    assert "重复" in r.json()["detail"]


def test_invalid_time_range_rejected(client):
    register_square(client, zone_id="co-r")
    _upload(client, "T1", CROSS_X)
    _upload(client, "T2", CROSS_Y)
    # since >= until
    r = _ask(client, "co-r", since="2026-09-30T10:02:00Z", until="2026-09-30T10:02:00Z")
    assert r.status_code == 400
    # 不带时区
    r = _ask(client, "co-r", since="2026-09-30T10:00:00", until="2026-09-30T10:02:00Z")
    assert r.status_code == 400
    assert "时区" in r.json()["detail"]
    # 非时间串
    r = _ask(client, "co-r", since="not-a-time")
    assert r.status_code == 400


def test_invalid_threshold_rejected(client):
    register_square(client, zone_id="co-th")
    _upload(client, "T1", CROSS_X)
    _upload(client, "T2", CROSS_Y)
    # 阈值 < 2
    r = _ask(client, "co-th", threshold=1)
    assert r.status_code in (400, 422)
    # 阈值 > 车次数
    r = _ask(client, "co-th", threshold=3)
    assert r.status_code == 400
    assert "阈值" in r.json()["detail"] or "threshold" in r.json()["detail"]
    # 非整数
    r = _ask(client, "co-th", threshold="two")
    assert r.status_code in (400, 422)


def test_invalid_request_shape_rejected(client):
    register_square(client, zone_id="co-s")
    # 车次不足两辆
    r = _ask(client, "co-s", trip_ids=["T1"])
    assert r.status_code in (400, 422)
    # 缺字段
    r = client.post("/api/v1/zones/co-s/co-occupancy", json={"trip_ids": ["T1", "T2"]})
    assert r.status_code == 422
    # 多余字段
    r = _ask(client, "co-s", bogus=1)
    assert r.status_code == 422


def test_invalid_track_versions_rejected(client):
    register_square(client, zone_id="co-tv")
    _upload(client, "T1", CROSS_X)
    _upload(client, "T2", CROSS_Y)
    # 版本号负数
    r = _ask(client, "co-tv", track_versions={"T1": -1})
    assert r.status_code in (400, 422)
    # 版本号非整数
    r = _ask(client, "co-tv", track_versions={"T1": "abc"})
    assert r.status_code in (400, 422)
    # 键不在 trip_ids 中
    r = _ask(client, "co-tv", track_versions={"T9": 1})
    assert r.status_code == 400
    assert "T9" in r.json()["detail"]


# ---------------------------------------------------------------------
# 同一已提交读视图：并发更新不拼出不存在的组合
# ---------------------------------------------------------------------

def test_concurrent_writes_never_break_snapshot(client):
    """writer 持续更新区域与轨迹，reader 反复查询：
    结果必须结构完整；固定版本组合的复现结果确定一致。"""
    register_square(client, zone_id="co-live")
    _upload(client, "T1", CROSS_X)
    _upload(client, "T2", CROSS_Y)

    stop = threading.Event()
    failures = []

    def writer() -> None:
        i = 0
        while not stop.is_set():
            size = 100 + (i % 3) * 50
            client.post(
                "/api/v1/zones",
                json={
                    "zone_id": "co-live",
                    "exterior": [[0, 0], [size, 0], [size, size], [0, size]],
                    "holes": [],
                    "windows": DAY_WINDOWS,
                },
            )
            client.post(
                "/api/v1/trips/T1/points",
                json={"points": [
                    {"point_id": f"extra-{i}", "ts": f"2026-09-30T12:{i % 60:02d}:00Z",
                     "x": 10 + i, "y": 10},
                ]},
            )
            i += 1

    def reader() -> None:
        while not stop.is_set():
            r = _ask(
                client, "co-live",
                since="2026-09-30T10:00:00Z", until="2026-09-30T10:02:00Z",
            )
            if r.status_code != 200:
                failures.append(("status", r.status_code, r.text))
                return
            body = r.json()
            trips = {t["trip_id"]: t for t in body["trips"]}
            # 回填的版本与点数必须来自同一快照：版本单调推进、点数与版本一致
            if trips["T1"]["track_version"] < 1 or trips["T2"]["track_version"] != 1:
                failures.append(("version-mix", body))
                return
            for seg in body["intervals"]:
                if seg["count"] != len(seg["trip_ids"]) or seg["count"] < 2:
                    failures.append(("segment", seg))
                    return
            # 固定组合复现：rev1 区域 × T1@1/T2@1，两次结果必须一致
            pinned = dict(
                since="2026-09-30T10:00:00Z", until="2026-09-30T10:02:00Z",
                revision=1, track_versions={"T1": 1, "T2": 1},
            )
            r1 = _ask(client, "co-live", **pinned)
            r2 = _ask(client, "co-live", **pinned)
            if r1.status_code != 200 or r1.json() != r2.json():
                failures.append(("pinned-nondeterministic", r1.status_code, r1.text))
                return
            if r1.json()["intervals"][0]["duration_sec"] != 40.0:
                failures.append(("pinned-wrong", r1.json()))
                return

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(2)]
    for t in threads:
        t.start()
    stop.wait(timeout=1.0)
    stop.set()
    for t in threads:
        t.join(timeout=10)

    assert not failures


def test_existing_single_trip_query_unaffected(client):
    """现有多车查询上线后，单车穿越查询语义不变。"""
    register_square(client, zone_id="co-keep")
    _upload(client, "T1", CROSS_X)
    r = client.get("/api/v1/trips/T1/crossings?zone_id=co-keep")
    assert r.status_code == 200
    z = r.json()["zones"][0]
    assert z["total_duration_sec"] == 40.0
    assert "track_version" not in r.json()
