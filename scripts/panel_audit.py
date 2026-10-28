#!/usr/bin/env python3
"""Аудит настроек панели 3x-ui / Xray (только чтение, ничего не меняет).

Запуск на сервере панели:  python3 panel_audit.py
Секреты (ключи, пароли, UUID, id, email) и IP-адреса скрываются: IP заменяются на IP#1, IP#2 ...
(одинаковый адрес — одинаковая метка), чтобы можно было проверить связи между входом и выходом.
"""
import json
import os
import re
import sqlite3
import subprocess
import sys

CONFIG_PATHS = ["/usr/local/x-ui/bin/config.json", "/etc/x-ui/config.json"]
DB_PATHS = ["/etc/x-ui/x-ui.db", "/usr/local/x-ui/x-ui.db"]
SECRET_KEYS = {"password", "privatekey", "publickey", "id", "uuid", "secretkey", "auth", "obfs", "psk",
               "token", "subid", "email", "pbk", "sid", "authpassword", "obfspassword", "key", "pass", "user"}
UUID_RE = re.compile(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}")
IP_RE = re.compile(r"\b(?:\d{1,3}\.){3}\d{1,3}\b")
_ips = {}
findings = []


def ip_label(m):
    ip = m.group(0)
    if ip in ("127.0.0.1", "0.0.0.0"):
        return ip
    return _ips.setdefault(ip, "IP#%d" % (len(_ips) + 1))


def clean(value):
    """Прячет секреты и IP во всей структуре."""
    if isinstance(value, dict):
        return {k: ("<скрыто>" if k.lower() in SECRET_KEYS else clean(v)) for k, v in value.items()}
    if isinstance(value, list):
        return [clean(v) for v in value]
    if isinstance(value, str):
        return IP_RE.sub(ip_label, UUID_RE.sub("<uuid>", value))
    return value


def warn(level, text):
    findings.append((level, text))


def sh(cmd, timeout=5):
    try:
        return subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout).stdout.strip()
    except Exception:
        return ""


def load_json(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def section(title):
    print("\n=== %s ===" % title)


def load_template():
    for p in DB_PATHS:
        if os.path.exists(p):
            try:
                con = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
                row = con.execute("SELECT value FROM settings WHERE key='xrayTemplateConfig'").fetchone()
                inbounds = con.execute("SELECT remark, enable, port, protocol, expiry_time FROM inbounds").fetchall()
                con.close()
                return (json.loads(row[0]) if row else None), inbounds
            except Exception as e:
                print("(не удалось прочитать базу %s: %s)" % (p, e))
    return None, []


def stream_summary(ss):
    ss = ss or {}
    sec = ss.get("security", "none")
    net = ss.get("network", "tcp")
    out = {"network": net, "security": sec}
    if sec == "reality":
        r = ss.get("realitySettings", {})
        out["dest"] = r.get("dest") or r.get("target")
        out["serverNames"] = r.get("serverNames") or r.get("serverName")  # у исходящего поле serverName
        out["shortIds"] = ("%d шт." % len(r.get("shortIds"))) if "shortIds" in r else ("задан" if r.get("shortId") else "НЕ ЗАДАН")
        out["fingerprint"] = (r.get("settings") or {}).get("fingerprint") or r.get("fingerprint")
    elif sec == "tls":
        t = ss.get("tlsSettings", {})
        out["serverName"] = t.get("serverName")
        out["alpn"] = t.get("alpn")
        out["fingerprint"] = (t.get("settings") or {}).get("fingerprint")
    if net == "xhttp":
        x = ss.get("xhttpSettings", {})
        out["xhttp"] = {"mode": x.get("mode"), "path": x.get("path")}
    elif net == "ws":
        out["ws"] = {"path": (ss.get("wsSettings") or {}).get("path")}
    return out


def main():
    cfg = None
    for p in CONFIG_PATHS:
        cfg = load_json(p)
        if cfg:
            print("Рабочий конфиг Xray:", p)
            break
    if not cfg:
        print("Не найден config.json Xray. Запусти скрипт на сервере панели от root.")
        sys.exit(1)
    template, db_inbounds = load_template()

    section("ВЕРСИИ И СЛУЖБЫ")
    print("xray:", (sh("/usr/local/x-ui/bin/xray-linux-amd64 -version | head -1") or "?"))
    print("x-ui служба:", sh("systemctl is-active x-ui") or "?")
    print("время синхронизировано:", sh("timedatectl show -p NTPSynchronized --value") or "?")

    section("ЖУРНАЛ И DNS")
    log = cfg.get("log") or {}
    print("log:", clean(log))
    if str(log.get("loglevel", "warning")).lower() in ("debug", "info"):
        warn("СОВЕТ", "loglevel=%s: журнал разрастается и хранит лишнее; для работы хватает warning" % log.get("loglevel"))
    if log.get("access") and log.get("access") != "none":
        warn("СОВЕТ", "включён access-журнал (%s): хранит адреса клиентов, на нагруженной ноде это диск и приватность" % log.get("access"))
    dns = cfg.get("dns")
    print("dns:", clean(dns) if dns else "нет (используется системный)")
    if not dns:
        warn("СОВЕТ", "блок dns не задан: запросы идут через системный резолвер; для каскада лучше DoH/DoT или свой DNS на выходной ноде")

    section("ВХОДЯЩИЕ (inbounds)")
    inbounds = cfg.get("inbounds") or []
    for ib in inbounds:
        tag = ib.get("tag")
        settings = ib.get("settings") or {}
        n_clients = len(settings.get("clients") or [])
        sniff = ib.get("sniffing") or {}
        line = {"tag": tag, "listen": ib.get("listen") or "0.0.0.0", "port": ib.get("port"),
                "protocol": ib.get("protocol"), "клиентов": n_clients,
                "sniffing": ("вкл " + str(sniff.get("destOverride"))) if sniff.get("enabled") else "выкл"}
        line.update(stream_summary(ib.get("streamSettings")))
        if ib.get("protocol") == "vless":
            flows = {c.get("flow") for c in settings.get("clients") or []}
            line["flow"] = sorted(f or "-" for f in flows)
        print(clean(line))
        if (tag or "").startswith("api") and (ib.get("listen") or "0.0.0.0") not in ("127.0.0.1", "::1"):
            warn("ВАЖНО", "api-inbound '%s' слушает не на 127.0.0.1: доступ к API Xray снаружи" % tag)
        if ib.get("protocol") in ("vless", "vmess", "trojan", "shadowsocks") and not sniff.get("enabled"):
            warn("ВАЖНО", "inbound '%s': sniffing выключен — правила маршрутизации по доменам (geosite, РФ напрямую) работать не будут" % tag)
        elif sniff.get("enabled") and ib.get("protocol") in ("vless", "vmess", "trojan", "shadowsocks"):
            if not sniff.get("routeOnly"):
                warn("СОВЕТ", "inbound '%s': sniffing без routeOnly подменяет адрес назначения; для каскада безопаснее routeOnly=true" % tag)
        ss = ib.get("streamSettings") or {}
        if ss.get("security") == "reality":
            r = ss.get("realitySettings", {})
            if not (r.get("shortIds") or []):
                warn("ВАЖНО", "inbound '%s': Reality без shortIds" % tag)
            if not (r.get("serverNames") or []):
                warn("ВАЖНО", "inbound '%s': Reality без serverNames" % tag)
        if ib.get("protocol") == "vless" and ss.get("security") == "none" and ss.get("network") == "tcp" \
                and ib.get("listen") not in ("127.0.0.1", "::1"):
            warn("ВАЖНО", "inbound '%s': VLESS без шифрования транспорта (security=none) смотрит наружу" % tag)

    section("ИСХОДЯЩИЕ (outbounds) — первый в списке работает по умолчанию")
    outbounds = cfg.get("outbounds") or []
    tags = []
    for i, ob in enumerate(outbounds):
        tags.append(ob.get("tag"))
        s = ob.get("settings") or {}
        info = {"№": i + 1, "tag": ob.get("tag"), "protocol": ob.get("protocol")}
        vn = s.get("vnext")
        if vn:
            info["куда"] = clean(["%s:%s" % (v.get("address"), v.get("port")) for v in vn])
        servers = s.get("servers")
        if servers:
            info["куда"] = clean(["%s:%s" % (v.get("address"), v.get("port")) for v in servers])
        if s.get("address"):
            info["куда"] = clean("%s:%s" % (s.get("address"), s.get("port")))
        info.update(stream_summary(ob.get("streamSettings")) if ob.get("streamSettings") else {})
        if ob.get("protocol") == "freedom" and s.get("domainStrategy"):
            info["domainStrategy"] = s.get("domainStrategy")
        print(clean(info))
    default_tag = outbounds[0].get("tag") if outbounds else None
    print("Исходящий по умолчанию:", default_tag)
    if "blocked" not in tags and not any(o.get("protocol") == "blackhole" for o in outbounds):
        warn("ВАЖНО", "нет blackhole-исходящего: нечем блокировать (торренты, приватные сети)")

    section("МАРШРУТИЗАЦИЯ (routing)")
    routing = cfg.get("routing") or {}
    print("domainStrategy:", routing.get("domainStrategy"))
    rules = routing.get("rules") or []
    blocks_private = blocks_bt = False
    for i, r in enumerate(rules):
        short = {"№": i + 1, "→": r.get("outboundTag") or r.get("balancerTag")}
        for k in ("inboundTag", "user", "protocol", "network", "port"):
            if r.get(k):
                short[k] = r[k]
        for k in ("domain", "ip", "source"):
            if r.get(k):
                vals = r[k]
                short[k] = (vals[:4] + ["… всего %d" % len(vals)]) if len(vals) > 4 else vals
        print(clean(short))
        tag_out = r.get("outboundTag")
        if tag_out and tag_out != "api" and tag_out not in tags and not r.get("balancerTag"):  # "api" встроен в Xray
            warn("ВАЖНО", "правило №%d ведёт в несуществующий outbound '%s'" % (i + 1, tag_out))
        if any("geoip:private" in str(x) for x in (r.get("ip") or [])) and tag_out in ("blocked", "block"):
            blocks_private = True
        if "bittorrent" in (r.get("protocol") or []) and tag_out in ("blocked", "block"):
            blocks_bt = True
    if not rules:
        warn("ВАЖНО", "нет правил маршрутизации")
    private_direct = any("geoip:private" in str(r.get("ip")) and r.get("outboundTag") == "direct" for r in rules)
    if private_direct:
        warn("ВАЖНО", "geoip:private отправлен в direct: клиент может достучаться до 127.0.0.1 и внутренних сервисов самой ноды (панель, API Xray). Лучше blocked")
    elif not blocks_private:
        warn("СОВЕТ", "нет правила geoip:private → blocked: клиент может обращаться к локальной сети сервера (127.0.0.1, 10.x, 192.168.x)")
    if not blocks_bt:
        warn("СОВЕТ", "нет правила bittorrent → blocked: торрент-трафик идёт через сервер (риск жалоб хостера)")
    if routing.get("domainStrategy") == "AsIs" and any(r.get("ip") for r in rules):
        warn("СОВЕТ", "domainStrategy=AsIs: правила по geoip для доменов не сработают (домен не резолвится); для каскада обычно IPIfNonMatch")
    if default_tag in ("direct",) and any(o.get("protocol") in ("vless", "vmess", "trojan", "shadowsocks", "wireguard", "socks", "hysteria") for o in outbounds):
        warn("ИНФО", "по умолчанию трафик идёт 'direct', а каскадный выход есть — убедись, что правила явно отправляют нужный трафик в выход (иначе он выходит с адреса этого сервера)")

    section("ШАБЛОН ПАНЕЛИ ПРОТИВ РАБОЧЕГО КОНФИГА")
    if template:
        def norm(c, part):
            v = c.get(part)
            if part == "routing" and isinstance(v, dict):  # панель сама добавляет api-правило при запуске
                v = dict(v, rules=[{k: x for k, x in r.items() if k != "enabled"}  # панель убирает enabled при генерации
                                   for r in v.get("rules") or [] if r.get("inboundTag") != ["api"]])
            return json.dumps(v, sort_keys=True)
        for part in ("outbounds", "routing", "dns"):
            same = norm(template, part) == norm(cfg, part)
            print("%-10s %s" % (part, "совпадает" if same else "ОТЛИЧАЕТСЯ"))
            if not same:
                warn("ВАЖНО", "%s в шаблоне панели (xrayTemplateConfig) и в рабочем config.json отличаются: при перезапуске панель перепишет конфиг из шаблона" % part)
    else:
        print("шаблон в базе не найден (или база недоступна)")
    if db_inbounds:
        print("в базе панели входящих:", len(db_inbounds), "| в рабочем конфиге (без api):", len([i for i in inbounds if not (i.get('tag') or '').startswith('api')]))

    section("СИСТЕМА")
    print("tcp congestion:", sh("sysctl -n net.ipv4.tcp_congestion_control"), "| qdisc:", sh("sysctl -n net.core.default_qdisc"))
    print("ip_forward:", sh("sysctl -n net.ipv4.ip_forward"))
    print("ufw:", (sh("ufw status | head -1") or "не установлен"))
    xp = sh("pidof xray-linux-amd64 | awk '{print $1}'")
    if xp:
        print("лимит открытых файлов у xray:", sh("grep 'open files' /proc/%s/limits" % xp))
    print("слушают порты (xray/x-ui):")
    print(clean(sh("ss -tlnpH 2>/dev/null | grep -E 'xray|x-ui' | awk '{print $4, $6}'") or "?"))
    if sh("timedatectl show -p NTPSynchronized --value") == "no":
        warn("СОВЕТ", "время не синхронизировано: от него зависят проверка TLS-сертификатов исходящих (WARP, Reality) и подписки; включи timedatectl set-ntp true")
    if "inactive" in sh("ufw status | head -1"):
        warn("СОВЕТ", "ufw выключен: порты панели и подписок открыты всем (если нет файрвола у хостера); оставь снаружи только порты клиентов и SSH")
    # правило-ловушка: если универсальное правило стоит выше точечных, они никогда не сработают
    for i, r in enumerate(rules):
        catch_all = r.get("outboundTag") and not any(r.get(k) for k in ("domain", "ip", "port", "protocol", "inboundTag", "source", "user"))
        if catch_all and i < len(rules) - 1:
            warn("ВАЖНО", "правило №%d ловит весь трафик, поэтому правила после него (№%d–%d) не сработают никогда" % (i + 1, i + 2, len(rules)))
            break
    if sh("sysctl -n net.ipv4.tcp_congestion_control") != "bbr":
        warn("СОВЕТ", "не включён BBR (tcp_congestion_control): для каскада с длинным каналом это заметно по скорости")

    section("ИТОГ")
    if not findings:
        print("замечаний нет")
    order = {"ВАЖНО": 0, "СОВЕТ": 1, "ИНФО": 2}
    for lvl, text in sorted(findings, key=lambda x: order.get(x[0], 9)):
        print("[%s] %s" % (lvl, text))


if __name__ == "__main__":
    main()
