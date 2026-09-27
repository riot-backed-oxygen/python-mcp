#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_ip.py —— 检测当前出口公网 IP 及归属信息

用法：
    python check_ip.py              # 查询所有源，汇总结论
    python check_ip.py -q           # 静默模式，只输出结果行
    python check_ip.py -t 5         # 设置单个请求超时（秒）
    python check_ip.py --json       # 以 JSON 输出，便于其他程序消费

只用标准库 urllib，无第三方依赖；如需更稳的重试可自行换 requests。
退出码：0 = 成功拿到 IP；1 = 全部源失败。
"""

import argparse
import concurrent.futures
import ipaddress
import json
import socket
import sys
import time
import urllib.error
import urllib.request

UA = "curl/8.0"  # 有些接口对浏览器 UA 返回 HTML，用 curl UA 拿纯文本更干净

# 每个源：(名称, URL, 解析函数)
SOURCES = [
    ("ipify",      "https://api.ipify.org?format=json",        lambda d: json.loads(d)["ip"]),
    ("ip-api",     "http://ip-api.com/json/?fields=status,message,query,country,regionName,city,isp,org,as,timezone",
                                                               lambda d: json.loads(d)),
    ("ipinfo",     "https://ipinfo.io/json",                   lambda d: json.loads(d)),
    ("ip.sb",      "https://api.ip.sb/geoip",                  lambda d: json.loads(d)),
    ("myip.ipip",  "https://myip.ipip.net/json",               lambda d: json.loads(d)),
    ("ifconfig.me","https://ifconfig.me/all.json",             lambda d: json.loads(d)),
]


def fetch(url: str, timeout: float = 8.0) -> str:
    """发一个 GET 请求，返回响应文本。"""
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept": "application/json"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return resp.read().decode("utf-8", "replace").strip()


def query(name: str, url: str, parser, timeout: float):
    """查询单个源，返回 (名称, 结果 或 None, 耗时, 错误信息)。"""
    start = time.perf_counter()
    try:
        raw = fetch(url, timeout)
        data = parser(raw) if parser else raw
        if isinstance(data, dict) and data.get("status") == "fail":
            raise ValueError(data.get("message", "接口返回失败"))
        return name, data, time.perf_counter() - start, None
    except Exception as exc:  # 网络错误、解析错误统一处理
        return name, None, time.perf_counter() - start, f"{type(exc).__name__}: {exc}"


def pick_ip(data):
    """从不同源的结构里尽量抽出 IP 字符串。"""
    if isinstance(data, str):
        return data.strip()
    if isinstance(data, dict):
        for key in ("ip", "query", "ip_addr", "IPv4", "ipv4"):
            if data.get(key):
                return str(data[key])
        # myip.ipip.net 的嵌套结构
        if isinstance(data.get("data"), dict):
            return pick_ip(data["data"])
    return None


def local_info():
    """顺便报一下本机出口 IP 与网卡地址，方便对比是否走了代理。"""
    info = {}
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 80))  # 不实际发包，只为让内核选出口网卡
        info["outbound_local_ip"] = s.getsockname()[0]
    except Exception:
        info["outbound_local_ip"] = None
    finally:
        s.close()
    try:
        info["hostname"] = socket.gethostname()
        info["resolved_ip"] = socket.gethostbyname(info["hostname"])
    except Exception:
        pass
    return info


def main():
    ap = argparse.ArgumentParser(description="检测当前公网 IP 及归属")
    ap.add_argument("-t", "--timeout", type=float, default=8.0, help="单个请求超时秒数")
    ap.add_argument("-q", "--quiet", action="store_true", help="只输出汇总结果")
    ap.add_argument("--json", action="store_true", help="以 JSON 形式输出")
    args = ap.parse_args()

    log = (lambda *a: None) if args.quiet or args.json else print

    log("=" * 66)
    log(f"公网 IP 检测  |  {time.strftime('%Y-%m-%d %H:%M:%S')}")
    log("=" * 66)

    results = {}
    # 并发查询，多个源互相校验（能发现某些源被劫持/缓存的情况）
    with concurrent.futures.ThreadPoolExecutor(max_workers=len(SOURCES)) as pool:
        futures = [pool.submit(query, n, u, p, args.timeout) for n, u, p in SOURCES]
        for fut in concurrent.futures.as_completed(futures):
            name, data, cost, err = fut.result()
            results[name] = {"data": data, "cost": round(cost, 2), "error": err}
            if err:
                log(f"  [失败] {name:<12} {cost:.2f}s  {err}")
            else:
                ip = pick_ip(data)
                log(f"  [成功] {name:<12} {cost:.2f}s  IP = {ip}")

    # 汇总：按出现次数投票，出现最多的即最可信的公网 IP
    ip_counts = {}
    for name, r in results.items():
        ip = pick_ip(r["data"]) if r["data"] else None
        if ip:
            try:
                ip = str(ipaddress.ip_address(ip.strip()))
            except ValueError:
                continue
            ip_counts.setdefault(ip, []).append(name)

    summary = {"timestamp": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
               "local": local_info(), "sources": results, "consensus": None}

    log("")
    if not ip_counts:
        log("结论：所有源均不可用，无法确定公网 IP（可能无外网或被防火墙拦截）。")
        if args.json:
            print(json.dumps(summary, ensure_ascii=False, indent=2))
        return 1

    best_ip, srcs = max(ip_counts.items(), key=lambda kv: len(kv[1]))
    detail = None
    for name in ("ip-api", "ipinfo", "ip.sb", "myip.ipip"):
        d = results.get(name, {}).get("data")
        if isinstance(d, dict):
            detail = (name, d)
            break

    log("-" * 66)
    log(f"公网 IP     : {best_ip}")
    log(f"一致源      : {len(srcs)}/{len(SOURCES)}  ({', '.join(srcs)})")
    if len(ip_counts) > 1:
        log(f"注意        : 检测到多个不同 IP，可能为多出口/代理池 -> {list(ip_counts)}")
    if detail:
        name, d = detail
        get = lambda *keys: next((d[k] for k in keys if d.get(k)), "-")
        log(f"归属地      : {get('country','country_name')} {get('regionName','region')} {get('city')}")
        log(f"运营商      : {get('isp','org')}  AS: {get('as','asn')}")
        log(f"时区        : {get('timezone','time_zone')}")
        log(f"数据来源    : {name}")

    li = summary["local"]
    if li.get("outbound_local_ip"):
        log(f"本机出口网卡 : {li['outbound_local_ip']} (与公网 IP 不同说明经过 NAT/代理)")
    log("-" * 66)

    summary["consensus"] = {"ip": best_ip, "sources": srcs, "count": len(srcs)}
    if args.json:
        print(json.dumps(summary, ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())
