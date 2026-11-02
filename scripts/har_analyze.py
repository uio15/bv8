#!/usr/bin/env python3
"""HAR / JSONL capture analyzer: first step of HTTP API reverse engineering.

把一次抓包压成「端点清单 + 参数变化量统计 + 可疑签名/加密字段定位」，
用来决定下一步去 JS 里搜哪个字段名、断哪个点。纯标准库，Windows / Linux 都能跑。

支持输入:
  - Chrome / Firefox / Charles / HttpToolkit 导出的 .har
  - 每行一个请求的 .jsonl: {"method": "...", "url": "...", "headers": {}, "body": "..."}

用法:
  python3 scripts/har_analyze.py capture.har
  python3 scripts/har_analyze.py capture.har --out report.md
  python3 scripts/har_analyze.py capture.har --only api.example.com --no-mask
  python3 scripts/har_analyze.py capture.har --json > report.json
"""

from __future__ import annotations

import argparse
import base64
import binascii
import json
import re
import sys
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Iterator
from urllib.parse import parse_qsl, unquote_plus, urlsplit

# 字段名/头名里出现这些词，就默认它值得优先看
SUSPECT_NAME = re.compile(
    r"(?:^|[_.\-])(sign|signature|sig|hmac|hash|digest|md5|sha\d*|token|nonce|"
    r"ts|timestamp|_t|key|api[-_]?key|secret|salt|iv|enc|encrypt|cipher|auth|"
    r"credential|verif|device|fingerprint|fp|trace|trace[-_]?id|request[-_]?id|"
    r"req[-_]?id|session)(?:$|[_.\-])",
    re.I,
)
NUM_RE = re.compile(r"^\d+$")
# 名字里直接点名「签名/HMAC/摘要」的，权重高于普通可疑词
SIGNATURE_NAME = re.compile(r"(sign|signature|sig|hmac|hash|digest|md5|sha\d*)", re.I)
# 这些名字的值一律打码，避免报告被转发时泄露凭据
SENSITIVE_NAME = re.compile(
    r"(pass|pwd|passwd|secret|token|cookie|authorization|api[-_]?key|otp|verify[-_]?code)", re.I
)
HEX_RE = re.compile(r"^[0-9a-fA-F]+$")
B64_RE = re.compile(r"^[A-Za-z0-9+/_\-]+={0,2}$")
UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$"
)
JWT_RE = re.compile(r"^[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]{6,}\.[A-Za-z0-9_-]*$")
# 路径里像 id 的段: /users/12345 或 /v1/9f8e.../detail
ID_SEGMENT = re.compile(r"/(?:\d{2,}|[0-9a-fA-F]{8,}|[0-9a-fA-F-]{16,})(?=/|$)")


def mask(value: str, keep: int = 12) -> str:
    """Truncate long values so reports can be pasted around safely."""
    value = value.replace("\n", "\\n").replace("\r", "")
    if len(value) <= keep + 6:
        return value
    return "%s...(%dB)" % (value[:keep], len(value))


def b64_like(value: str) -> bool:
    if len(value) < 12 or not B64_RE.match(value):
        return False
    if HEX_RE.match(value) and len(value) % 2 == 0:
        return False
    padded = value + "=" * (-len(value) % 4)
    try:
        raw = base64.urlsafe_b64decode(padded.replace("+", "-").replace("/", "_"))
    except (binascii.Error, ValueError):
        return False
    if not raw:
        return False
    # 真实 payload 一般大部分是可见字符或结构清晰的二进制
    printable = sum(1 for byte in raw if 9 <= byte <= 126)
    return printable / len(raw) > 0.6 or b64_like_structured(raw)


def b64_like_structured(raw: bytes) -> bool:
    return raw[:1] in (b"\x1f", b"\x08", b"{", b"[") or raw.count(b"\x00") > 0


def as_epoch(value: str):
    """Return UTC datetime if the value plausibly is a unix timestamp."""
    if not NUM_RE.match(value) or len(value) not in (10, 13, 16):
        return None
    number = int(value)
    seconds = number / (1000 if len(value) == 13 else 1e6 if len(value) == 16 else 1)
    try:
        stamp = datetime.fromtimestamp(seconds, tz=timezone.utc)
    except (OverflowError, OSError, ValueError):
        return None
    return stamp if 2001 <= stamp.year <= 2100 else None


def value_kind(value: str, total: int, distinct: int) -> list[str]:
    """Cheap heuristics that tell the reader what a field probably is."""
    tags: list[str] = []
    if distinct == 1 and total > 1:
        return ["常量"]
    stamp = as_epoch(value)
    if stamp is not None:
        tags.append("unix 时间戳(%s)" % stamp.strftime("%Y-%m-%d %H:%M:%S UTC"))
    if UUID_RE.match(value):
        tags.append("UUID")
    if JWT_RE.match(value):
        tags.append("JWT")
    if HEX_RE.match(value) and len(value) >= 16 and len(value) % 2 == 0:
        tags.append("hex %d 位" % len(value))
    elif b64_like(value):
        tags.append("base64/加密块 %d 位" % len(value))
    if not tags:
        if NUM_RE.match(value):
            tags.append("数字")
        elif len(value) > 300:
            tags.append("长文本(%dB)" % len(value))
        else:
            tags.append("字符串")
    return tags


def flatten_json(value: Any, prefix: str = "", depth: int = 0) -> Iterator[tuple[str, str]]:
    """Flatten nested JSON into (dotted.path, scalar string) pairs."""
    if depth > 6:
        yield prefix, json.dumps(value, ensure_ascii=False)[:200]
        return
    if isinstance(value, dict):
        for key, item in value.items():
            yield from flatten_json(item, "%s.%s" % (prefix, key) if prefix else str(key), depth + 1)
    elif isinstance(value, list):
        for index, item in enumerate(value[:5]):
            yield from flatten_json(item, "%s[%d]" % (prefix, index) if prefix else "[%d]" % index, depth + 1)
    elif value is None:
        yield prefix, ""
    elif isinstance(value, bool):
        yield prefix, "true" if value else "false"
    else:
        yield prefix, str(value)


def norm_path(path: str) -> str:
    return ID_SEGMENT.sub("/{id}", path or "/")


def parse_body(body: str, content_type: str) -> list[tuple[str, str]]:
    """Return flattened (name, value) pairs for a request body."""
    body = (body or "").strip()
    if not body:
        return []
    if "json" in content_type or body[:1] in ("{", "["):
        try:
            return list(flatten_json(json.loads(body)))
        except ValueError:
            pass
    if "x-www-form-urlencoded" in content_type or ("=" in body and "&" in body):
        pairs = []
        for chunk in body.split("&"):
            if not chunk:
                continue
            name, _, val = chunk.partition("=")
            try:
                val = unquote_plus(val)
            except Exception:
                pass
            pairs.append((name, val))
        return pairs
    return [("<raw body>", body)]


def iter_har(log: dict) -> Iterator[dict]:
    for entry in log.get("entries") or []:
        request = entry.get("request") or {}
        response = entry.get("response") or {}
        headers = {str(h.get("name", "")).lower(): str(h.get("value", ""))
                   for h in request.get("headers") or []}
        resp_headers = {str(h.get("name", "")).lower(): str(h.get("value", ""))
                        for h in response.get("headers") or []}
        yield {
            "method": str(request.get("method", "GET")).upper(),
            "url": str(request.get("url", "")),
            "headers": headers,
            "body": str((request.get("postData") or {}).get("text") or ""),
            "req_ct": headers.get("content-type", ""),
            "status": int(response.get("status") or 0),
            "resp_ct": resp_headers.get("content-type", ""),
            "resp_body": str(((response.get("content") or {}).get("text")) or ""),
            "started": str(entry.get("startedDateTime", "")),
        }


def iter_jsonl(text: str) -> Iterator[dict]:
    for line in text.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        item = json.loads(line)
        if not isinstance(item, dict):
            continue
        headers = {str(k).lower(): str(v) for k, v in (item.get("headers") or {}).items()}
        yield {
            "method": str(item.get("method", "GET")).upper(),
            "url": str(item.get("url", "")),
            "headers": headers,
            "body": str(item.get("body") or ""),
            "req_ct": headers.get("content-type", str(item.get("content_type") or "")),
            "status": int(item.get("status") or 0),
            "resp_ct": str(item.get("resp_content_type") or ""),
            "resp_body": str(item.get("resp_body") or ""),
            "started": str(item.get("started") or ""),
        }


def load_capture(path: str) -> list[dict]:
    with open(path, "r", encoding="utf-8-sig") as handle:
        text = handle.read()
    if text.lstrip().startswith("{"):
        document = json.loads(text)
        if isinstance(document, dict) and "log" in document:
            return list(iter_har(document["log"]))
        return list(iter_jsonl(text))
    return list(iter_jsonl(text))

MAX_VALUES = 40


def _add(bucket: list[str], value: str) -> None:
    if len(bucket) < MAX_VALUES:
        bucket.append(value)


def aggregate(entries: list[dict]) -> dict:
    endpoints: dict[str, dict] = {}
    hosts: set[str] = set()
    statuses: dict[int, int] = defaultdict(int)
    for req in entries:
        parts = urlsplit(req["url"])
        host = parts.hostname or ""
        hosts.add(host)
        statuses[req["status"]] += 1
        path = norm_path(parts.path)
        key = "%s %s%s" % (req["method"], parts.netloc, path)
        ep = endpoints.get(key)
        if ep is None:
            ep = endpoints[key] = {
                "key": key, "method": req["method"], "host": parts.netloc, "path": path,
                "count": 0, "statuses": defaultdict(int), "req_ct": set(), "resp_ct": set(),
                "resp_keys": set(), "params": defaultdict(list), "headers": defaultdict(list),
                "blob_resp": 0,
            }
        ep["count"] += 1
        ep["statuses"][req["status"]] += 1
        if req["req_ct"]:
            ep["req_ct"].add(req["req_ct"].split(";")[0].strip())
        if req["resp_ct"]:
            ep["resp_ct"].add(req["resp_ct"].split(";")[0].strip())
        for name, value in parse_qsl(parts.query, keep_blank_values=True):
            _add(ep["params"][("query", name)], value)
        for name, value in parse_body(req["body"], req["req_ct"]):
            _add(ep["params"][("body", name)], value)
        for name, value in req["headers"].items():
            if name in ("content-length", "host", "connection"):
                continue
            _add(ep["headers"][name], value)
        _absorb_response(ep, req)
    return {"endpoints": endpoints, "hosts": sorted(hosts),
            "statuses": dict(statuses), "total": len(entries)}


def _absorb_response(ep: dict, req: dict) -> None:
    body = (req.get("resp_body") or "").strip()
    if not body:
        return
    if body[:1] in ("{", "["):
        try:
            doc = json.loads(body)
        except ValueError:
            ep["blob_resp"] += 1
            return
        if isinstance(doc, dict):
            ep["resp_keys"].update(list(doc.keys())[:12])
        return
    if HEX_RE.match(body) and len(body) > 64:
        ep["blob_resp"] += 1
    elif b64_like(body) and len(body) > 64:
        ep["blob_resp"] += 1


def score_row(name: str, values: list[str]) -> tuple[int, int, list[str]]:
    distinct = len(set(values))
    tags = value_kind(values[-1], len(values), distinct)
    score = 0
    if SIGNATURE_NAME.search(name):
        score += 5
    elif SUSPECT_NAME.search(name):
        score += 3
    if distinct > 1:
        score += 1
    if any(tag.startswith("unix") for tag in tags):
        score += 1
    if any(tag.startswith(("hex", "base64")) for tag in tags):
        score += 1
    return score, distinct, tags


def _cell(text: str, width: int = 60) -> str:
    text = str(text).replace("|", "\\|").replace("\n", " ")
    return text if len(text) <= width else text[: width - 1] + "~"


def sample_out(name: str, value: str, keep_secrets: bool) -> str:
    """Mask credential-looking values even when they are short."""
    if keep_secrets:
        return value
    if SENSITIVE_NAME.search(name):
        return "*** (%dB)" % len(value)
    return mask(value)


def build_report(agg: dict, keep_secrets: bool, min_score: int, max_samples: int) -> tuple[str, dict]:
    endpoints = sorted(agg["endpoints"].values(), key=lambda ep: -ep["count"])
    suspect: list[dict] = []
    dynamic: list[dict] = []
    const: list[dict] = []

    for ep in endpoints:
        rows = [(loc, name, values) for (loc, name), values in ep["params"].items()]
        rows += [("header", name, values) for name, values in ep["headers"].items()]
        for loc, name, values in rows:
            score, distinct, tags = score_row(name, values)
            item = {
                "endpoint": ep["key"], "location": loc, "name": name,
                "occurrences": len(values), "distinct": distinct,
                "kind": ", ".join(tags), "score": score,
                "samples": [sample_out(name, v, keep_secrets) for v in values[-max_samples:]],
            }
            if score >= min_score:
                suspect.append(item)
            elif distinct > 1:
                dynamic.append(item)
            elif loc != "header" and item["occurrences"] > 1:
                const.append(item)

    suspect.sort(key=lambda it: (-it["score"], -it["distinct"], it["name"]))
    dynamic.sort(key=lambda it: (it["endpoint"], it["name"]))

    lines: list[str] = []
    lines.append("# 抓包分析报告")
    lines.append("")
    lines.append("- 请求总数: **%d**" % agg["total"])
    lines.append("- 主机: %s" % (", ".join("`%s`" % h for h in agg["hosts"]) or "-"))
    lines.append("- 状态码: %s" % (", ".join("%s×%d" % (k, v) for k, v in sorted(agg["statuses"].items())) or "-"))
    lines.append("- 去重后端点: **%d**" % len(endpoints))
    lines.append("")
    lines.append("## 1. 端点清单")
    lines.append("")
    lines.append("| # | 端点 | 次数 | 状态 | 请求体类型 | 响应类型 | 响应顶层字段 |")
    lines.append("| - | --- | - | - | --- | --- | --- |")
    for index, ep in enumerate(endpoints, 1):
        lines.append("| %d | `%s` | %d | %s | %s | %s | %s |" % (
            index, _cell(ep["key"], 48), ep["count"],
            "/".join(str(k) for k in sorted(ep["statuses"])),
            ", ".join(sorted(ep["req_ct"])) or "-",
            ", ".join(sorted(ep["resp_ct"])) or "-",
            ", ".join(sorted(ep["resp_keys"])[:8]) or "-"))
    lines.append("")
    lines.append("## 2. 高优先级字段（签名 / 时间戳 / 加密载荷候选）")
    lines.append("")
    if suspect:
        lines.append("| 端点 | 位置 | 字段 | 出现/取值 | 判定 | 样例 |")
        lines.append("| --- | - | --- | --- | --- | --- |")
        for item in suspect:
            lines.append("| `%s` | %s | `%s` | %d/%d | %s | %s |" % (
                _cell(item["endpoint"], 40), item["location"], _cell(item["name"], 28),
                item["occurrences"], item["distinct"], item["kind"],
                _cell(" ; ".join(item["samples"]), 56)))
    else:
        lines.append("_没有命中高优先级规则。可能抓包条数太少，建议同一操作重复 3 次以上再抓。_")
    lines.append("")
    lines.append("## 3. 其余动态字段（每请求变化，低优先级）")
    lines.append("")
    for item in dynamic[:30]:
        lines.append("- `%s` %s `%s` — %d 次 / %d 种取值, %s" % (
            _cell(item["endpoint"], 40), item["location"], item["name"],
            item["occurrences"], item["distinct"], item["kind"]))
    if len(dynamic) > 30:
        lines.append("- ... 另外 %d 个，用 `--json` 看全量" % (len(dynamic) - 30))
    if not dynamic:
        lines.append("_无_")
    lines.append("")
    lines.append("## 4. 常量字段（多次请求取值不变，多半是硬编码/固定配置）")
    lines.append("")
    if const:
        const.sort(key=lambda it: (it["endpoint"], it["name"]))
        for item in const[:20]:
            lines.append("- `%s` %s `%s` = %s" % (
                _cell(item["endpoint"], 40), item["location"], item["name"],
                _cell(item["samples"][-1], 48)))
        if len(const) > 20:
            lines.append("- ... 另外 %d 个，用 `--json` 看全量" % (len(const) - 20))
    else:
        lines.append("_无_")
    lines.append("")
    lines.append("## 5. 疑似加密/压缩响应")
    lines.append("")
    blobs = [ep for ep in endpoints if ep["blob_resp"]]
    if blobs:
        for ep in blobs:
            lines.append("- `%s` — %d/%d 条响应不是明文 JSON" % (ep["key"], ep["blob_resp"], ep["count"]))
    else:
        lines.append("_响应都是明文，无需先解混淆。_")
    lines.append("")
    lines.append("## 6. 下一步")
    lines.append("")
    if suspect:
        top = suspect[0]
        lines.append("1. 在页面 JS 里全局搜字段名 %s，定位它的赋值处。" % top["name"])
        lines.append("2. 在该处下断点，比对 `%s` 的输入（通常拼接了 path + 时间戳 + nonce + 固定盐）。" % top["name"])
        lines.append("3. 把算法抄成 Python（`hashlib` / `hmac` 即可覆盖绝大多数 sign），用抓包里的同一条请求做回归校验。")
    else:
        lines.append("1. 同一操作重复抓 3 次以上，让动态字段暴露出来。")
        lines.append("2. 确认是否走 WebSocket / gRPC（HAR 里会表现为 Upgrade 或 application/grpc）。")
    return "\n".join(lines) + "\n", {"suspect": suspect, "dynamic": dynamic, "const": const,
                                     "endpoints": [ep["key"] for ep in endpoints]}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="HAR / JSONL 抓包分析器（接口逆向第一步）")
    parser.add_argument("capture", help=".har 或 .jsonl 抓包文件")
    parser.add_argument("--out", help="把 markdown 报告写入文件")
    parser.add_argument("--json", action="store_true", help="输出 JSON 而不是 markdown")
    parser.add_argument("--only", help="只分析 URL 匹配该正则的请求")
    parser.add_argument("--max-samples", type=int, default=2, help="每个字段展示的样例数，默认 2")
    parser.add_argument("--min-score", type=int, default=4, help="进入高优先级表的分数阈值，默认 4")
    parser.add_argument("--no-mask", action="store_true", help="不截断样例值（可能包含敏感信息）")
    args = parser.parse_args(argv)

    entries = load_capture(args.capture)
    if args.only:
        pattern = re.compile(args.only, re.I)
        entries = [entry for entry in entries if pattern.search(entry["url"])]
    if not entries:
        print("没有可分析的请求。", file=sys.stderr)
        return 2

    agg = aggregate(entries)
    markdown, data = build_report(agg, args.no_mask, args.min_score, args.max_samples)
    if args.json:
        text = json.dumps({
            "total": agg["total"], "hosts": agg["hosts"], "statuses": agg["statuses"],
            "endpoints": data["endpoints"], "suspect": data["suspect"], "dynamic": data["dynamic"],
            "const": data["const"],
        }, ensure_ascii=False, indent=2)
    else:
        text = markdown

    if args.out:
        with open(args.out, "w", encoding="utf-8") as handle:
            handle.write(text)
        print("报告已写入 %s（%d 条请求 / %d 个端点）" % (args.out, agg["total"], len(data["endpoints"])))
    else:
        print(text)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
