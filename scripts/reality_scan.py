#!/usr/bin/env python3
"""Сканер целей (dest) для REALITY — подбор сайтов под российские серверы.

Запускать НА СЕРВЕРЕ, для которого выбираешь цель (вход в России): задержка
меряется именно с него. Только чтение: сканер делает обычные TLS-рукопожатия
на порт 443, ничего не меняет и ничего не ставит.

Что значит «хорошая цель» для REALITY:
  * TLS 1.3 и HTTP/2 (h2) — иначе сервер просто не подойдёт;
  * действительный сертификат, выданный на этот домен (SAN), срок не меньше 14 дней;
  * X25519 (проверяется, если на сервере есть openssl);
  * маленькая задержка с твоего сервера;
  * сайт находится в России и не требует Cloudflare/зарубежных CDN.

Примеры:
  python3 reality_scan.py                          # встроенный список российских сайтов
  python3 reality_scan.py --file мои_домены.txt    # свой список, по одному домену в строке
  python3 reality_scan.py --neighbors 203.0.113.0/24   # сайты по соседству (та же сеть хостера)
  python3 reality_scan.py --top 15 --json result.json

Только стандартная библиотека Python 3.8+.
"""
import argparse
import asyncio
import ipaddress
import json
import shutil
import socket
import ssl
import statistics
import subprocess
import sys
import tempfile
import time

DEFAULT_DOMAINS = """
yandex.ru ya.ru dzen.ru market.yandex.ru kinopoisk.ru vk.com vk.ru ok.ru mail.ru rambler.ru
avito.ru ozon.ru wildberries.ru megamarket.ru dns-shop.ru citilink.ru mvideo.ru eldorado.ru lamoda.ru
sber.ru sberbank.ru tbank.ru vtb.ru alfabank.ru gazprombank.ru banki.ru sravni.ru yoomoney.ru
gosuslugi.ru mos.ru nalog.gov.ru cbr.ru nspk.ru mironline.ru pochta.ru cdek.ru
ivi.ru okko.tv rutube.ru 2gis.ru hh.ru drom.ru auto.ru cian.ru domclick.ru
lenta.ru ria.ru rbc.ru kommersant.ru tass.ru gazeta.ru kp.ru sports.ru championat.com
mts.ru beeline.ru megafon.ru tele2.ru rt.ru rzd.ru aeroflot.ru s7.ru tutu.ru ostrovok.ru
pikabu.ru habr.com vc.ru max.ru
""".split()

MIN_DAYS_LEFT = 14


def _ctx(verify):
    ctx = ssl.create_default_context() if verify else ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT)
    if not verify:
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
    ctx.minimum_version = ssl.TLSVersion.TLSv1_2
    ctx.set_alpn_protocols(["h2", "http/1.1"])
    return ctx


async def handshake(host, port, timeout, server_name=None, verify=True):
    """Одно рукопожатие. Возвращает (мс, ssl_object) либо бросает исключение."""
    t0 = time.perf_counter()
    reader, writer = await asyncio.wait_for(
        asyncio.open_connection(host, port, ssl=_ctx(verify), server_hostname=server_name), timeout)
    ms = (time.perf_counter() - t0) * 1000
    obj = writer.get_extra_info("ssl_object")
    info = {
        "tls": obj.version(),
        "alpn": obj.selected_alpn_protocol(),
        "cert": obj.getpeercert() if verify else None,
        "der": obj.getpeercert(binary_form=True),
    }
    writer.close()
    try:
        await writer.wait_closed()
    except Exception:
        pass
    return ms, info


async def check_domain(domain, port, tries, timeout, sem):
    res = {"domain": domain, "ok": False, "why": ""}
    async with sem:
        try:
            infos = await asyncio.get_running_loop().getaddrinfo(domain, port, family=socket.AF_INET, type=socket.SOCK_STREAM)
            ip = infos[0][4][0]
        except Exception:
            res["why"] = "не резолвится"
            return res
        res["ip"] = ip
        times, info = [], None
        for _ in range(tries):
            try:
                ms, info = await handshake(ip, port, timeout, server_name=domain)
                times.append(ms)
            except ssl.SSLCertVerificationError:
                res["why"] = "сертификат не прошёл проверку (не на этот домен или просрочен)"
                return res
            except (asyncio.TimeoutError, OSError):
                res["why"] = "нет ответа на 443"
                return res
            except ssl.SSLError as e:
                res["why"] = "ошибка TLS: %s" % getattr(e, "reason", e)
                return res
    if info["tls"] != "TLSv1.3":
        res["why"] = "нет TLS 1.3 (%s)" % info["tls"]
        return res
    if info["alpn"] != "h2":
        res["why"] = "нет HTTP/2 (h2)"
        return res
    cert = info["cert"] or {}
    left = (ssl.cert_time_to_seconds(cert["notAfter"]) - time.time()) / 86400 if cert.get("notAfter") else None
    if left is not None and left < MIN_DAYS_LEFT:
        res["why"] = "сертификат скоро истекает (%d дн.)" % left
        return res
    san = [v for k, v in cert.get("subjectAltName", ()) if k == "DNS"]
    res.update(ok=True, ms=statistics.median(times), san=san, days=int(left) if left is not None else None,
               issuer=dict(x[0] for x in cert.get("issuer", ())).get("organizationName", "?"))
    return res


def x25519_ok(domain, ip):
    """True/False, либо None, если openssl недоступен."""
    if not shutil.which("openssl"):
        return None
    try:
        out = subprocess.run(
            ["openssl", "s_client", "-connect", "%s:443" % ip, "-servername", domain, "-tls1_3", "-groups", "X25519"],
            input=b"", capture_output=True, timeout=10)
        text = out.stdout.decode("utf-8", "ignore") + out.stderr.decode("utf-8", "ignore")
        return "X25519" in text and "TLSv1.3" in text
    except Exception:
        return None


def decode_der_names(der):
    """DNS-имена из сертификата без проверки (для поиска по соседним IP)."""
    try:
        pem = ssl.DER_cert_to_PEM_cert(der)
        with tempfile.NamedTemporaryFile("w", suffix=".pem") as f:
            f.write(pem)
            f.flush()
            cert = ssl._ssl._test_decode_cert(f.name)  # закрытая функция stdlib, поэтому в try
        return [v for k, v in cert.get("subjectAltName", ()) if k == "DNS" and not v.startswith("*")]
    except Exception:
        return []


async def neighbors(cidr, port, timeout, sem):
    net = ipaddress.ip_network(cidr, strict=False)
    if net.num_addresses > 1024:
        sys.exit("Сеть слишком большая: возьми /22 или меньше")
    found = set()

    async def one(ip):
        async with sem:
            try:
                _, info = await handshake(str(ip), port, timeout, verify=False)
            except Exception:
                return
            found.update(decode_der_names(info["der"]))

    await asyncio.gather(*(one(ip) for ip in net.hosts()))
    return sorted(found)


def xray_hint(r):
    names = []
    for n in r["san"]:
        if not n.startswith("*") and n not in names:
            names.append(n)
    if r["domain"] not in names:
        names.insert(0, r["domain"])
    return {"dest": "%s:443" % r["domain"], "serverNames": names[:8]}


async def main_async(args):
    domains = []
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            domains = [x.strip().lower() for x in f if x.strip() and not x.startswith("#")]
    else:
        domains = list(DEFAULT_DOMAINS)
    sem = asyncio.Semaphore(args.parallel)
    if args.neighbors:
        print("Ищу сайты в сети %s ..." % args.neighbors)
        near = await neighbors(args.neighbors, args.port, args.timeout, sem)
        print("  найдено доменов в сертификатах соседей: %d" % len(near))
        domains += near
    domains = list(dict.fromkeys(domains))
    print("Проверяю %d доменов (рукопожатий на домен: %d) ..." % (len(domains), args.tries))
    results = await asyncio.gather(*(check_domain(d, args.port, args.tries, args.timeout, sem) for d in domains))
    good = sorted((r for r in results if r["ok"]), key=lambda r: r["ms"])
    bad = [r for r in results if not r["ok"]]

    for r in good[:args.top]:
        r["x25519"] = x25519_ok(r["domain"], r["ip"])
    shown = [r for r in good if r.get("x25519") is not False][:args.top] if shutil.which("openssl") else good[:args.top]

    print("\n%-28s %-16s %7s %5s  %s" % ("ДОМЕН", "IP", "мс", "дней", "X25519 / издатель"))
    for r in shown:
        x = {True: "да", False: "нет", None: "?"}[r.get("x25519")]
        print("%-28s %-16s %7.0f %5s  %s / %s" % (r["domain"], r["ip"], r["ms"], r["days"] if r["days"] is not None else "-", x, r["issuer"]))
    if not shown:
        print("(подходящих целей нет)")
    else:
        best = shown[0]
        print("\nЛучшая цель — подставь в Reality на этом сервере:")
        print(json.dumps(xray_hint(best), ensure_ascii=False, indent=2))
        print("Имена в serverNames взяты из сертификата цели, поэтому подходят к ней. Задержка меряется с этого сервера.")
    if args.show_bad and bad:
        print("\nОтсеяны:")
        for r in bad:
            print("  %-28s %s" % (r["domain"], r["why"]))
    else:
        print("\nОтсеяно: %d (--show-bad покажет причины)" % len(bad))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as f:
            json.dump({"good": [dict(r, hint=xray_hint(r)) for r in shown], "rejected": bad}, f, ensure_ascii=False, indent=2)
        print("Результат записан в %s" % args.json)


def main():
    p = argparse.ArgumentParser(description="Сканер целей REALITY")
    p.add_argument("--file", help="файл со списком доменов (по одному в строке)")
    p.add_argument("--neighbors", metavar="CIDR", help="проверить сайты в соседней сети (например, /24 твоего сервера)")
    p.add_argument("--top", type=int, default=15, help="сколько лучших показать (15)")
    p.add_argument("--tries", type=int, default=3, help="рукопожатий на домен для замера задержки (3)")
    p.add_argument("--timeout", type=float, default=5.0, help="таймаут, секунд (5)")
    p.add_argument("--parallel", type=int, default=40, help="одновременных проверок (40)")
    p.add_argument("--port", type=int, default=443)
    p.add_argument("--show-bad", action="store_true", help="показать, почему отсеяны остальные")
    p.add_argument("--json", metavar="ФАЙЛ", help="записать результат в JSON")
    args = p.parse_args()
    asyncio.run(main_async(args))


if __name__ == "__main__":
    main()
