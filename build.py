#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
Рендер «Химического новостного радара» из issue.json в HTML и PDF.
Облачная версия (Claude Code Routine) — пути относительно репозитория,
плюс отправка готового PDF по почте. Локальная версия с путями
~/.claude/roshim-radar и ~/Desktop/Росхим-Радар/ живёт отдельно.

Использование:
    python3 build.py [путь_к_issue.json]

По умолчанию читает issue.json рядом со скриптом и кладёт результат в
./out/ : ДайджестРХ-<ДД-ММ-ГГГГ>.html/.pdf + ДайджестРХ-latest.html/.pdf,
затем шлёт PDF по почте (см. send_email).

Дизайн (LLM собирает данные — этот скрипт только форматирует, чтобы HTML и PDF были
согласованы). См. config.md.
"""
import json, os, re, sys, shutil, html, smtplib, ssl
from datetime import date
from email.message import EmailMessage

RADAR_DIR = os.path.dirname(os.path.abspath(__file__))
OUT_DIR = os.path.join(RADAR_DIR, "out")
DEFAULT_ISSUE = os.path.join(RADAR_DIR, "issue.json")

# --------- палитра (светлая, общая для HTML и PDF) ----------
C = {
    "ink": "#15201D", "ink2": "#3C4945", "muted": "#6A7874",
    "accent": "#0B6B64", "accent_ink": "#084F49",
    "hi": "#B23A2E", "med": "#A86B0C", "watch": "#2C6E9B",
    "line": "#DBE2DF", "line_strong": "#C6D0CC",
    "surface2": "#F5F8F6",
    "hi_bg": "#f6e4e1", "med_bg": "#f4e9d3", "watch_bg": "#dfe9f2",
}
IMPACT_COLOR = {"hi": C["hi"], "med": C["med"], "watch": C["watch"]}
IMPACT_BG = {"hi": C["hi_bg"], "med": C["med_bg"], "watch": C["watch_bg"]}


# =========================================================================
#  ВСПОМОГАТЕЛЬНОЕ
# =========================================================================
def he(s):
    """HTML-escape."""
    return html.escape(str(s), quote=True)


_EMOJI_RE = re.compile(
    "["
    "\U0001F000-\U0001FAFF"   # эмодзи-пиктограммы (вне BMP)
    "\U00002600-\U000027BF"   # misc symbols + dingbats (⚡🌍🏭 частично, ✂ и т.п.)
    "\U00002B00-\U00002BFF"
    "\U0000FE00-\U0000FE0F"   # variation selectors
    "\U0001F1E6-\U0001F1FF"   # флаги
    "]", flags=re.UNICODE)


def strip_emoji(s):
    """Убрать эмодзи для PDF (шрифт Arimo их не рисует). Стрелки ↑↓→▲▼ сохраняются."""
    return _EMOJI_RE.sub("", str(s)).replace("  ", " ").strip()


# =========================================================================
#  КОНТРОЛЬ КАЧЕСТВА ВЫПУСКА (жёсткий, на уровне кода — не на дисциплине промпта)
# =========================================================================
MAX_AGE_NEWS_DAYS = 3       # новостные рубрики 1–5: не старше 72 ч
MAX_AGE_REGION_DAYS = 30    # карточки регионов: не старше месяца
MAX_AGE_TICKER_DAYS = 2     # плитки тикера (Brent, статусы проливов)
HISTORY_WINDOW_DAYS = 14    # окно антиповтора
HISTORY_KEEP_DAYS = 30      # глубина журнала
MIN_ITEMS_WARN = 5          # ниже этого выпуск подозрительно пустой

# SEO-витрины «отчётов о рынке»: вечнозелёный текст без реальной даты события
BLOCKED_DOMAINS = (
    "imarcgroup", "mordorintelligence", "procurementresource", "expertmarketresearch",
    "marketreportsworld", "coherentmarketinsights", "price-watch", "chemtradeasia",
    "marketdataforecast", "evolvancemarketresearch", "intellectia", "papaverai",
    "grandviewresearch", "marketsandmarkets", "researchandmarkets", "precedenceresearch",
    "blackridgeresearch", "futuremarketinsights", "fortunebusinessinsights",
)

# Набор и порядок рубрик задаёт КОД. От агента берутся только items.
SECTION_SPEC = [
    ("urgent",   "⚡",  "Срочное",                  "Срочное / высокое влияние",
     "Главные события за сутки с прямым эффектом на рынок — мировые и российские."),
    ("geo",      "🌍",  "Геополитика · логистика",  "Геополитика и международная логистика",
     "Конфликты, торговые пути, фрахт, страховки, порты."),
    ("capacity", "🏭",  "Зарубежные мощности",      "Зарубежные мощности и производства",
     "Пуски, закрытия, расширения, ремонты, аварии, форс-мажоры вне РФ."),
    ("reg",      "⚖️",  "Зарубежное регулирование", "Зарубежное регулирование и санкции",
     "Пошлины, квоты, антидемпинг, санкции, экология, торговые барьеры вне РФ."),
    ("energy",   "🛢️",  "Мировая энергетика",       "Мировая энергетика и сырьевые рынки",
     "Нефть, газ, электроэнергия, фидстоки — драйверы себестоимости химии."),
    ("ru",       "🇷🇺",  "Российский рынок",         "Российский рынок",
     "Регулирование и экспортные ограничения РФ, M&A, проекты и мощности внутри страны."),
]

# Внутренний рынок РФ освещается в рубрике «Российский рынок». В «Срочное» такие
# новости попадать могут, из зарубежных рубрик 2–5 — переносятся сюда автоматически.
RU_ONLY_SECTION = "ru"
FOREIGN_SECTIONS = ("geo", "capacity", "reg", "energy")
RU_MARKET_RE = re.compile(
    r"минпромторг|минэнерго|минсельхоз|минфин\s+рф|правительств\w*\s+рф|российск\w*\s+правительств|"
    r"кабмин|госдум|совет\w*\s+федерации|фас\s+росси|фтс\s+росси|ростехнадзор|росприроднадзор|"
    r"ржд|мишустин|новак|постановлени\w*\s+правительств|указ\s+президент|"
    r"цб\s+рф|банк\s+росси|биржевы\w*\s+торг\w*\s+спбмтсб|спбмтсб", re.I)

REGION_SPEC = [
    ("Китай", "🇨🇳", "закреплён"), ("ЛатАм", "🌎", "закреплён"),
    ("Африка", "🌍", "закреплён"), ("MENA", "🕌", "закреплён"),
    ("Европа", "🇪🇺", "обзорно"),  ("Сев. Америка", "🇺🇸", "обзорно"),
]
PINNED_REGIONS = {n for n, _, r in REGION_SPEC if r == "закреплён"}

IMPACT_LABEL = {"hi": "Высокое", "med": "Среднее", "watch": "Следим"}
DIRS_OK = {"up", "down", "flat"}

_MONTHS_GEN = ["января", "февраля", "марта", "апреля", "мая", "июня",
               "июля", "августа", "сентября", "октября", "ноября", "декабря"]
_WEEKDAYS = ["понедельник", "вторник", "среда", "четверг", "пятница",
             "суббота", "воскресенье"]


def human_date(dt):
    return f"{_WEEKDAYS[dt.weekday()]}, {dt.day} {_MONTHS_GEN[dt.month - 1]} {dt.year}"


def dmy(dt):
    """ДД.ММ.ГГГГ — основной формат дат в выпуске."""
    return f"{dt.day:02d}.{dt.month:02d}.{dt.year}"


DOC_TITLE = "Дайджест ключевых новостей"
FILE_PREFIX = "ДайджестРХ"        # имена файлов: ДайджестРХ-ДД-ММ-ГГГГ.pdf / .html


def _pdate(v):
    """Безопасный разбор ISO-даты; None, если её нет или она битая."""
    try:
        return date.fromisoformat(str(v))
    except Exception:
        return None


def fmt_age(pub, today):
    """Дата новости, показываемая над заголовком."""
    return dmy(pub)


def _host(url):
    m = re.match(r"https?://([^/]+)", str(url or ""))
    return (m.group(1) if m else "").lower()


def _blocked(url):
    h = _host(url)
    return next((b for b in BLOCKED_DOMAINS if b in h), None)


def _canon_url(u):
    return str(u or "").split("?")[0].split("#")[0].rstrip("/").lower()


def _norm_title(t):
    return re.sub(r"[^0-9a-zа-яё]+", " ", str(t or "").lower()).strip()


# ---------- нормализация: структуру выпуска задаёт код, не агент ----------
def normalize_issue(d, ref):
    """
    Код владеет датой, нумерацией, названиями рубрик, набором карточек регионов
    и подписями важности — чтобы агент физически не мог их разъехать.
    """
    warns = []
    if d.get("date_iso") != ref.isoformat():
        warns.append(f"date_iso в issue.json = {d.get('date_iso')!r}, а расчётная дата {ref} "
                     f"— беру расчётную (иначе старое проходило бы как свежее)")
    d["date_iso"] = ref.isoformat()
    d["date_human"] = human_date(ref)
    d["date_dmy"] = dmy(ref)
    if not d.get("regions_line"):
        d["regions_line"] = "Китай · ЛатАм · Африка · MENA + ЕС/США"

    by_id = {s.get("id"): s for s in d.get("sections", []) if isinstance(s, dict)}
    secs = []
    for i, (sid, emoji, toc, title, note) in enumerate(SECTION_SPEC, 1):
        src = by_id.pop(sid, {})
        secs.append({"id": sid, "num": f"{i:02d}", "emoji": emoji, "toc": toc,
                     "title": title, "note": src.get("note") or note,
                     "items": [x for x in src.get("items", []) if isinstance(x, dict)]})
    for sid, s in by_id.items():
        warns.append(f"неизвестная рубрика {sid!r}: {len(s.get('items', []))} пункт(ов) НЕ попали в выпуск")

    # внутрироссийские сюжеты не должны растекаться по зарубежным рубрикам
    by_sid = {s["id"]: s for s in secs}
    ru_sec = by_sid.get(RU_ONLY_SECTION)
    if ru_sec is not None:
        for sid in FOREIGN_SECTIONS:
            sec = by_sid.get(sid)
            if not sec:
                continue
            stay = []
            for it in sec["items"]:
                blob = f"{it.get('title', '')} {it.get('why', '')}"
                if RU_MARKET_RE.search(blob):
                    ru_sec["items"].append(it)
                    warns.append(f"«{str(it.get('title', ''))[:60]}» перенесён из "
                                 f"{sid} в «Российский рынок» (внутренний рынок РФ)")
                else:
                    stay.append(it)
            sec["items"] = stay
    d["sections"] = secs

    cards = {c.get("name"): c for c in d.get("regions_cards", []) if isinstance(c, dict)}
    out = []
    for name, flag, role in REGION_SPEC:
        src = cards.pop(name, {})
        out.append({"name": name, "flag": flag, "role": role,
                    "bullets": [b for b in src.get("bullets", []) if isinstance(b, dict)]})
    for name in cards:
        warns.append(f"неизвестная карточка региона {name!r} — пропущена")
    d["regions_cards"] = out

    # тренды: показываемая подпись считается из updated_iso
    tr = d.get("trends") or {}
    ti = _pdate(tr.get("updated_iso"))
    if ti:
        tr["updated"] = f"обновлено: {ti.day} {_MONTHS_GEN[ti.month - 1]} {ti.year}"
        if (ref - ti).days > 7:
            warns.append(f"тренды не обновлялись {(ref - ti).days} дн (нужно по понедельникам)")
    elif tr.get("points"):
        warns.append("в trends.json нет updated_iso — возраст трендов не проверяется")
    d["trends"] = tr
    return warns


# ---------- журнал освещённого ----------
def load_history(ref):
    """URL и заголовки за последние HISTORY_WINDOW_DAYS дней (сегодняшние — не в счёт)."""
    path = os.path.join(RADAR_DIR, "history.jsonl")
    urls, titles = set(), set()
    if not os.path.exists(path):
        return urls, titles
    with open(path, encoding="utf-8") as f:
        for ln in f:
            ln = ln.strip()
            if not ln:
                continue
            try:
                rec = json.loads(ln)
            except Exception:
                continue
            dt = _pdate(rec.get("date"))
            if not dt or dt >= ref or (ref - dt).days > HISTORY_WINDOW_DAYS:
                continue
            if rec.get("url"):
                urls.add(_canon_url(rec["url"]))
            if rec.get("title"):
                titles.add(_norm_title(rec["title"]))
    return urls, titles


def append_history(d, ref):
    """Журнал ведёт КОД — и только по тому, что реально попало в выпуск."""
    path = os.path.join(RADAR_DIR, "history.jsonl")
    rows = []
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            for ln in f:
                ln = ln.strip()
                if not ln:
                    continue
                try:
                    rows.append(json.loads(ln))
                except Exception:
                    pass
    seen = {(r.get("date"), _canon_url(r.get("url"))) for r in rows}
    added = 0
    for sec in d["sections"]:
        for it in sec["items"]:
            key = (d["date_iso"], _canon_url(it.get("source_url")))
            if key in seen:
                continue
            seen.add(key)
            rows.append({"date": d["date_iso"], "section": sec["id"],
                         "title": it.get("title", ""), "url": it.get("source_url", ""),
                         "gist": str(it.get("why", ""))[:220]})
            added += 1
    rows = [r for r in rows
            if _pdate(r.get("date")) is None or (ref - _pdate(r.get("date"))).days <= HISTORY_KEEP_DAYS]
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")
    return added


# ---------- свежесть, источники, антиповтор ----------
def enforce_freshness(d, ref, hist_urls, hist_titles):
    """
    Выбрасывает всё, что нельзя показывать: без даты, старое, из будущего,
    без источника, из блок-листа SEO-витрин, уже показанное на днях.
    Поля age / impact_label ВСЕГДА вычисляются здесь (агент их не задаёт).
    Возвращает список выброшенного: (где, что, причина).
    """
    dropped = []

    for sec in d["sections"]:
        kept = []
        for it in sec["items"]:
            title = str(it.get("title", ""))[:70]
            где = sec["id"]
            url = it.get("source_url", "")

            if not url:
                dropped.append((где, title, "нет ссылки на источник"))
                continue
            b = _blocked(url)
            if b:
                dropped.append((где, title, f"источник в блок-листе витрин отчётов ({b})"))
                continue

            p = _pdate(it.get("pub_date"))
            if not p:
                dropped.append((где, title, "нет корректного pub_date"))
                continue
            if p > ref:
                dropped.append((где, title, f"pub_date в будущем ({p})"))
                continue

            # дата события: статья могла выйти раньше самого события
            # (вступление пошлин в силу, старт ремонта, закрытие тендера)
            eff = p
            e = _pdate(it.get("event_date"))
            if e and eff < e <= ref:
                eff = e

            n = (ref - eff).days
            if n > MAX_AGE_NEWS_DAYS:
                dropped.append((где, title, f"старше {MAX_AGE_NEWS_DAYS} дн (событие {eff})"))
                continue

            if not it.get("update"):
                if _canon_url(url) in hist_urls:
                    dropped.append((где, title, "этот же URL уже был в выпуске за последние 14 дн"))
                    continue
                if _norm_title(it.get("title")) in hist_titles:
                    dropped.append((где, title, "этот сюжет уже показывали (нет пометки update)"))
                    continue

            imp = it.get("impact")
            if imp not in IMPACT_LABEL:
                imp = "watch"
            it["impact"] = imp
            it["impact_label"] = IMPACT_LABEL[imp]
            it["age"] = fmt_age(eff, ref)
            kept.append(it)
        sec["items"] = kept

    # карточки регионов: пункт СО ССЫЛКОЙ обязан иметь дату; без ссылки — синтез/контекст
    for rc in d["regions_cards"]:
        kept = []
        for bl in rc["bullets"]:
            text = str(bl.get("text", ""))[:70]
            url = bl.get("url")
            if not url:
                kept.append(bl)
                continue
            b = _blocked(url)
            if b:
                dropped.append((rc["name"], text, f"источник в блок-листе ({b})"))
                continue
            p = _pdate(bl.get("pub_date"))
            if not p:
                dropped.append((rc["name"], text, "ссылка без pub_date"))
                continue
            if p > ref:
                dropped.append((rc["name"], text, f"pub_date в будущем ({p})"))
                continue
            if (ref - p).days > MAX_AGE_REGION_DAYS:
                dropped.append((rc["name"], text, f"старше {MAX_AGE_REGION_DAYS} дн (опубл. {p})"))
                continue
            kept.append(bl)
        rc["bullets"] = kept

    # тикер: Brent и статусы проливов — самое заметное место, устаревать им нельзя
    tk = []
    for t in d.get("ticker", []):
        if not isinstance(t, dict) or not t.get("k") or not t.get("v"):
            continue
        label = str(t.get("k", ""))[:40]
        p = _pdate(t.get("pub_date"))
        if not p:
            dropped.append(("тикер", label, "нет pub_date"))
            continue
        if p > ref or (ref - p).days > MAX_AGE_TICKER_DAYS:
            dropped.append(("тикер", label, f"значение от {p} — старше {MAX_AGE_TICKER_DAYS} дн"))
            continue
        if t.get("dir") not in DIRS_OK:
            t["dir"] = "flat"
        tk.append(t)
    d["ticker"] = tk

    return dropped


# =========================================================================
#  HTML
# =========================================================================
CSS = """
:root{
 --font-sans:-apple-system,BlinkMacSystemFont,"Segoe UI","Helvetica Neue",Arial,sans-serif;
 --font-mono:ui-monospace,"SF Mono",Menlo,Consolas,"Roboto Mono",monospace;
 --bg:#EDF1EF;--surface:#FFFFFF;--surface-2:#F5F8F6;--ink:#15201D;--ink-2:#3C4945;
 --muted:#6A7874;--line:#DBE2DF;--line-strong:#C6D0CC;--accent:#0B6B64;--accent-ink:#084F49;
 --hi:#B23A2E;--med:#A86B0C;--watch:#2C6E9B;--good:#2E7D46;
 --hi-bg:#f6e4e1;--med-bg:#f4e9d3;--watch-bg:#dfe9f2;
 --shadow:0 1px 2px rgba(17,30,27,.04),0 6px 18px rgba(17,30,27,.05);--radius:12px;--maxw:940px;}
@media (prefers-color-scheme:dark){:root{
 --bg:#0C1311;--surface:#141D1A;--surface-2:#101815;--ink:#E7EDEA;--ink-2:#B7C2BE;
 --muted:#869390;--line:#232F2B;--line-strong:#2E3C37;--accent:#33B3AA;--accent-ink:#7CD8D0;
 --hi:#E4705F;--med:#D69A3E;--watch:#5AA0D0;--good:#5FC17E;
 --hi-bg:#2a1a17;--med-bg:#2a2314;--watch-bg:#14212c;
 --shadow:0 1px 2px rgba(0,0,0,.3),0 8px 24px rgba(0,0,0,.28);}}
*{box-sizing:border-box}body{margin:0}
.wrap{font-family:var(--font-sans);background:var(--bg);color:var(--ink);line-height:1.5;
 -webkit-font-smoothing:antialiased;padding:clamp(14px,3vw,34px) clamp(12px,3vw,24px) 64px;min-height:100vh}
.shell{max-width:var(--maxw);margin:0 auto}
a{color:var(--accent-ink)}
.masthead{border:1px solid var(--line);background:var(--surface);border-radius:var(--radius);
 box-shadow:var(--shadow);overflow:hidden}
.masthead__top{padding:clamp(18px,3vw,28px) clamp(18px,3vw,30px) 20px;border-bottom:1px solid var(--line)}
.eyebrow{font-family:var(--font-mono);font-size:11px;letter-spacing:.14em;text-transform:uppercase;
 color:var(--accent);font-weight:600;display:flex;align-items:center;gap:8px;flex-wrap:wrap}
.eyebrow .dot{width:7px;height:7px;border-radius:50%;background:var(--hi);animation:pulse 2.4s ease-out infinite}
@keyframes pulse{0%{box-shadow:0 0 0 0 rgba(178,58,46,.45)}70%{box-shadow:0 0 0 7px rgba(178,58,46,0)}100%{box-shadow:0 0 0 0 rgba(178,58,46,0)}}
h1.title{font-size:clamp(27px,5.2vw,44px);line-height:1.05;letter-spacing:-.02em;margin:12px 0 0;font-weight:800;text-wrap:balance}
.subtitle{margin:10px 0 0;color:var(--ink-2);font-size:clamp(14px,1.6vw,15.5px);max-width:62ch}
.issue-meta{margin-top:16px;font-family:var(--font-mono);font-size:12.5px;color:var(--muted);
 display:flex;gap:8px 16px;flex-wrap:wrap;font-variant-numeric:tabular-nums}
.issue-meta b{color:var(--ink);font-weight:600}
.ticker{display:grid;grid-template-columns:repeat(auto-fit,minmax(150px,1fr));background:var(--surface-2)}
.tk{padding:12px 16px;border-right:1px solid var(--line);border-top:1px solid var(--line)}
.tk:first-child{border-top:none}
.tk__k{font-family:var(--font-mono);font-size:10.5px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted)}
.tk__v{font-family:var(--font-mono);font-weight:600;font-size:15px;margin-top:3px;
 font-variant-numeric:tabular-nums;display:flex;align-items:baseline;gap:7px}
.tk__v small{font-size:11px;font-weight:600}
.up{color:var(--hi)}.down{color:var(--good)}.flat{color:var(--muted)}
nav.toc{display:flex;flex-wrap:wrap;gap:8px;margin:22px 0 6px}
.toc a{font-family:var(--font-mono);font-size:12px;text-decoration:none;color:var(--ink-2);
 border:1px solid var(--line-strong);background:var(--surface);padding:6px 11px;border-radius:999px;
 transition:border-color .15s,color .15s}
.toc a:hover{border-color:var(--accent);color:var(--accent-ink)}
section.sec{margin-top:30px;scroll-margin-top:16px}
.sec__head{display:flex;align-items:center;gap:12px;margin-bottom:4px}
.sec__head h2{font-size:clamp(17px,2.4vw,21px);margin:0;font-weight:750;letter-spacing:-.01em}
.sec__num{font-family:var(--font-mono);font-size:12px;color:var(--accent);border:1px solid var(--accent);
 border-radius:6px;padding:2px 7px;font-weight:600;flex:none}
.sec__rule{height:1px;background:var(--line);flex:1}
.sec__note{color:var(--muted);font-size:13px;margin:2px 0 14px}
.items{display:flex;flex-direction:column;gap:10px}
.item{display:grid;grid-template-columns:4px 1fr;background:var(--surface);border:1px solid var(--line);
 border-radius:10px;overflow:hidden;box-shadow:var(--shadow)}
.item__bar{background:var(--watch)}
.item--hi .item__bar{background:var(--hi)}.item--med .item__bar{background:var(--med)}.item--watch .item__bar{background:var(--watch)}
.item__body{padding:13px 16px 14px;min-width:0}
.item__meta{display:flex;flex-wrap:wrap;gap:6px;margin-bottom:8px;align-items:center}
.chip{font-family:var(--font-mono);font-size:10.5px;letter-spacing:.04em;padding:2px 8px;border-radius:999px;font-weight:600;white-space:nowrap}
.chip--impact{text-transform:uppercase}
.chip--hi{background:var(--hi-bg);color:var(--hi)}.chip--med{background:var(--med-bg);color:var(--med)}.chip--watch{background:var(--watch-bg);color:var(--watch)}
.chip--dir{background:transparent;color:var(--ink-2);border:1px solid var(--line-strong)}
.chip--upd{background:transparent;color:var(--accent-ink);border:1px solid var(--accent);text-transform:uppercase}
.chip--age{margin-left:auto;background:transparent;color:var(--muted);border:1px dashed var(--line-strong)}
.item__title{font-size:15.5px;line-height:1.35;margin:0;font-weight:680;letter-spacing:-.005em;text-wrap:pretty}
.item__why{margin:7px 0 0;font-size:13.5px;color:var(--ink-2)}
.why-label{font-family:var(--font-mono);font-size:11px;letter-spacing:.04em;text-transform:uppercase;color:var(--accent);font-weight:600;margin-right:5px}
.item__tags{display:flex;flex-wrap:wrap;align-items:center;gap:6px;margin-top:11px}
.tag{font-family:var(--font-mono);font-size:11px;padding:2px 8px;border-radius:5px;border:1px solid var(--line-strong);color:var(--muted);background:var(--surface-2)}
.tag--region{color:var(--accent-ink);border-color:color-mix(in srgb,var(--accent) 35%,var(--line-strong))}
.src{font-family:var(--font-mono);font-size:11.5px;margin-left:auto;text-decoration:none;color:var(--accent-ink);border-bottom:1px solid transparent;white-space:nowrap}
.src:hover{border-bottom-color:var(--accent)}
.quiet{font-size:13.5px;color:var(--muted);font-style:italic;padding:12px 16px;border:1px dashed var(--line-strong);border-radius:10px;background:var(--surface-2)}
.regiongrid{display:grid;gap:10px;grid-template-columns:repeat(auto-fit,minmax(270px,1fr))}
.rcard{background:var(--surface);border:1px solid var(--line);border-radius:10px;padding:15px 16px 16px;box-shadow:var(--shadow)}
.rcard h3{margin:0 0 4px;font-size:15px;font-weight:720;display:flex;align-items:center;gap:8px}
.rcard .flag{font-size:17px}
.rcard .rrole{font-family:var(--font-mono);font-size:10px;letter-spacing:.08em;text-transform:uppercase;color:var(--muted);font-weight:600;border:1px solid var(--line-strong);padding:1px 6px;border-radius:4px;margin-left:auto}
.rcard ul{margin:10px 0 0;padding-left:0;list-style:none;display:flex;flex-direction:column;gap:8px}
.rcard li{font-size:13.3px;color:var(--ink-2);padding-left:15px;position:relative}
.rcard li::before{content:"";position:absolute;left:0;top:8px;width:5px;height:5px;border-radius:50%;background:var(--accent)}
.rcard li a{text-decoration:none;border-bottom:1px solid var(--line-strong)}
.rcard li a:hover{border-bottom-color:var(--accent)}
.trends{margin-top:12px;background:var(--surface);border:1px solid var(--line);border-radius:10px;box-shadow:var(--shadow);padding:16px 18px 18px}
.trends .updated{font-family:var(--font-mono);font-size:11px;letter-spacing:.06em;text-transform:uppercase;color:var(--muted);margin:0 0 12px}
.trend{padding:11px 0;border-top:1px solid var(--line)}
.trend:first-of-type{border-top:none;padding-top:0}
.trend h4{margin:0 0 4px;font-size:14.5px;font-weight:700}
.trend p{margin:0;font-size:13.3px;color:var(--ink-2)}
.legend{margin-top:26px;background:var(--surface-2);border:1px dashed var(--line-strong);border-radius:10px;padding:14px 16px;display:grid;gap:10px 22px;grid-template-columns:repeat(auto-fit,minmax(210px,1fr))}
.legend h4{grid-column:1/-1;margin:0;font-family:var(--font-mono);font-size:11px;letter-spacing:.1em;text-transform:uppercase;color:var(--muted)}
.legrow{display:flex;align-items:flex-start;gap:9px;font-size:12.5px;color:var(--ink-2)}
.swatch{width:4px;align-self:stretch;min-height:30px;border-radius:2px;flex:none}
.sw-hi{background:var(--hi)}.sw-med{background:var(--med)}.sw-watch{background:var(--watch)}
footer.foot{margin-top:30px;border-top:1px solid var(--line);padding-top:18px;color:var(--muted);font-size:12.5px}
footer.foot b{color:var(--ink-2)}
footer.foot .badge{font-family:var(--font-mono);font-size:11px;color:var(--accent);border:1px solid var(--accent);border-radius:6px;padding:2px 8px;display:inline-block;margin-bottom:10px}
footer.foot p{margin:8px 0 0;max-width:82ch}
@media (max-width:560px){.item__title{font-size:15px}.src{margin-left:0}}
@media (prefers-reduced-motion:reduce){.eyebrow .dot{animation:none}*{scroll-behavior:auto!important}}
"""


def _chip_dir(text):
    return f'<span class="chip chip--dir">{he(text)}</span>'


def render_item_html(it):
    imp = it.get("impact", "watch")
    meta = [f'<span class="chip chip--impact chip--{imp}">{he(it.get("impact_label",""))}</span>']
    if it.get("update"):
        meta.append('<span class="chip chip--upd">Обновление</span>')
    for d in it.get("dirs", []):
        meta.append(_chip_dir(d))
    if it.get("age"):
        meta.append(f'<span class="chip chip--age">{he(it["age"])}</span>')
    tags = []
    for r in it.get("regions", []):
        tags.append(f'<span class="tag tag--region">{he(r)}</span>')
    for p in it.get("products", []):
        tags.append(f'<span class="tag">{he(p)}</span>')
    src = ""
    if it.get("source_url"):
        src = (f'<a class="src" href="{he(it["source_url"])}" target="_blank" '
               f'rel="noopener">{he(it.get("source_name","Источник"))} ↗</a>')
    return f'''      <article class="item item--{imp}">
        <div class="item__bar"></div>
        <div class="item__body">
          <div class="item__meta">{''.join(meta)}</div>
          <h3 class="item__title">{he(it.get("title",""))}</h3>
          <p class="item__why"><span class="why-label">Почему важно</span>{he(it.get("why",""))}</p>
          <div class="item__tags">{''.join(tags)}{src}</div>
        </div>
      </article>'''


def render_section_html(sec):
    if sec.get("items"):
        inner = "\n".join(render_item_html(i) for i in sec["items"])
        body = f'      <div class="items">\n{inner}\n      </div>'
    else:
        body = '      <div class="quiet">Существенных событий за сутки нет.</div>'
    note = f'<p class="sec__note">{he(sec["note"])}</p>' if sec.get("note") else ""
    emoji = (sec.get("emoji","") + " ") if sec.get("emoji") else ""
    return f'''    <section class="sec" id="{he(sec.get("id",""))}">
      <div class="sec__head"><span class="sec__num">{he(sec.get("num",""))}</span><h2>{emoji}{he(sec.get("title",""))}</h2><span class="sec__rule"></span></div>
      {note}
{body}
    </section>'''


def render_html(d):
    # тикер
    tk = ""
    for t in d.get("ticker", []):
        dr = t.get("dir") if t.get("dir") in DIRS_OK else "flat"
        small = f'<small class="{dr}">{he(t["d"])}</small>' if t.get("d") else ""
        tk += (f'<div class="tk"><div class="tk__k">{he(t["k"])}</div>'
               f'<div class="tk__v {dr}">{he(t["v"])} {small}</div></div>')
    # TOC
    toc = ""
    for sec in d["sections"]:
        lbl = (sec.get("emoji","") + " " + sec.get("toc", sec.get("title",""))).strip()
        toc += f'<a href="#{he(sec["id"])}">{he(lbl)}</a>'
    toc += '<a href="#regions">📍 Новости по регионам</a><a href="#trends">📈 Тренды за месяц</a>'
    # секции новостей
    secs = "\n".join(render_section_html(s) for s in d["sections"])
    # регионы
    rcards = ""
    for rc in d.get("regions_cards", []):
        bl = "".join(
            f'<li>{he(b["text"])}' + (f' <a href="{he(b["url"])}" target="_blank" rel="noopener">источник</a>' if b.get("url") else "") + '</li>'
            for b in rc.get("bullets", []))
        flag = f'<span class="flag">{he(rc.get("flag",""))}</span> ' if rc.get("flag") else ""
        rcards += (f'<div class="rcard"><h3>{flag}{he(rc["name"])} '
                   f'<span class="rrole">{he(rc.get("role",""))}</span></h3><ul>{bl}</ul></div>')
    # тренды
    tr = d.get("trends", {})
    trpoints = "".join(
        f'<div class="trend"><h4>{he(p["title"])}</h4><p>{he(p["text"])}</p></div>'
        for p in tr.get("points", []))
    trblock = (f'''    <section class="sec" id="trends">
      <div class="sec__head"><span class="sec__num">{he(str(len(d["sections"])+2)).zfill(2)}</span><h2>📈 Тренды мировой химотрасли за месяц</h2><span class="sec__rule"></span></div>
      <div class="trends"><p class="updated">{he(tr.get("updated",""))}</p>{trpoints}</div>
    </section>''') if tr.get("points") else ""

    return f'''<title>{he(DOC_TITLE)} за {he(d.get("date_dmy",""))} — АО «Росхим»</title>
<style>{CSS}</style>
<div class="wrap"><div class="shell">
    <header class="masthead">
      <div class="masthead__top">
        <div class="eyebrow"><span class="dot"></span> АО «РОСХИМ» · Департамент международного развития</div>
        <h1 class="title">{he(DOC_TITLE)} за {he(d.get("date_dmy",""))}</h1>
        <p class="subtitle">Ежедневная сводка событий, влияющих на спрос, предложение и цены в химотрасли — по продуктовому периметру компании и закреплённым регионам.</p>
        <div class="issue-meta">
          <span>Обновление: <b>Пн–Пт, 08:00 МСК</b></span>
        </div>
      </div>
      <div class="ticker">{tk}</div>
    </header>
    <nav class="toc">{toc}</nav>
{secs}
    <section class="sec" id="regions">
      <div class="sec__head"><span class="sec__num">{str(len(d["sections"])+1).zfill(2)}</span><h2>📍 Новости по регионам</h2><span class="sec__rule"></span></div>
      <p class="sec__note">Закреплённые регионы — детально; Европа и Сев. Америка — обзорно, как драйверы глобального баланса.</p>
      <div class="regiongrid">{rcards}</div>
    </section>
{trblock}
    <div class="legend">
      <h4>Как читать</h4>
      <div class="legrow"><span class="swatch sw-hi"></span><div><b style="color:var(--hi)">Высокое</b> — прямой и заметный эффект на рынок; реагировать в первую очередь.</div></div>
      <div class="legrow"><span class="swatch sw-med"></span><div><b style="color:var(--med)">Среднее</b> — существенно для планирования, но без немедленной срочности.</div></div>
      <div class="legrow"><span class="swatch sw-watch"></span><div><b style="color:var(--watch)">Следим</b> — фоновый тренд или раннее событие на контроле.</div></div>
    </div>
    <footer class="foot">
      <span class="badge">ЕЖЕДНЕВНО · ПН–ПТ · ДЛЯ ВНУТРЕННЕГО ПОЛЬЗОВАНИЯ</span>
      <p><b>Методология.</b> Сводка собирается автоматически по открытым источникам и ранжируется по влиянию на спрос, предложение и цены химии. Приоритет отбора — продуктовый периметр АО «Росхим» плюс широкий охват отрасли. Окно свежести — 24–72 ч, повторяющиеся сюжеты помечаются как «Обновление».</p>
      <p><b>Ограничения.</b> Работает по открытому вебу; полнотекстовые премиум-данные и «живые» котировки за пейволом недоступны. Для торговых решений сверяйтесь с первичными источниками и вашими подписками.</p>
    </footer>
</div></div>'''


# =========================================================================
#  PDF (reportlab)
# =========================================================================
# Знак «Росхим» — файл рядом с этим скриптом (не на рабочем столе, чтобы выпуск
# не сломался, если картинку оттуда уберут). Пропорции берутся из самого файла.
LOGO_PATH = os.path.join(RADAR_DIR, "logo.png")


def draw_logo(canv, right, top, width):
    """Знак «Росхим», прижатый правым верхним углом к точке (right, top)."""
    if not os.path.exists(LOGO_PATH):
        return
    try:
        from reportlab.lib.utils import ImageReader
        img = ImageReader(LOGO_PATH)
        iw, ih = img.getSize()
        height = width * ih / iw
        canv.drawImage(img, right - width, top - height, width, height, mask="auto")
    except Exception as e:
        print(f"⚠ логотип не отрисован ({LOGO_PATH}): {e}")


def build_pdf(d, path):
    from reportlab.lib.pagesizes import A4
    from reportlab.lib.units import mm
    from reportlab.lib import colors
    from reportlab.pdfbase import pdfmetrics
    from reportlab.pdfbase.ttfonts import TTFont
    from reportlab.platypus import (SimpleDocTemplate, Paragraph, Spacer, Table,
                                    TableStyle, KeepTogether, Flowable)
    from reportlab.lib.styles import ParagraphStyle
    from reportlab.lib.enums import TA_LEFT

    F = os.path.join(RADAR_DIR, "fonts") + os.sep
    pdfmetrics.registerFont(TTFont("AR", F + "Arimo-Regular.ttf"))
    pdfmetrics.registerFont(TTFont("AR-B", F + "Arimo-Bold.ttf"))
    pdfmetrics.registerFont(TTFont("AR-I", F + "Arimo-Italic.ttf"))

    def esc(s):
        s = strip_emoji(str(s))
        return s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")

    def urlesc(u):
        return str(u).replace("&", "&amp;").replace('"', "%22")

    ink, ink2, muted = colors.HexColor(C["ink"]), colors.HexColor(C["ink2"]), colors.HexColor(C["muted"])
    accent = colors.HexColor(C["accent"]); accent_ink = colors.HexColor(C["accent_ink"])
    line = colors.HexColor(C["line"])

    st = ParagraphStyle
    S = {
        "eyebrow": st("eyebrow", fontName="AR-B", fontSize=7.5, textColor=accent, leading=10, spaceAfter=2),
        "title": st("title", fontName="AR-B", fontSize=19, textColor=ink, leading=22, spaceAfter=3),
        "subtitle": st("subtitle", fontName="AR", fontSize=9.5, textColor=ink2, leading=13, spaceAfter=6),
        "meta": st("meta", fontName="AR", fontSize=8, textColor=muted, leading=12),
        "sec": st("sec", fontName="AR-B", fontSize=13, textColor=ink, leading=16, spaceBefore=12, spaceAfter=1),
        "note": st("note", fontName="AR-I", fontSize=8.5, textColor=muted, leading=11, spaceAfter=5),
        "metaline": st("metaline", fontName="AR-B", fontSize=7.5, leading=10, spaceAfter=2),
        "itemtitle": st("itemtitle", fontName="AR-B", fontSize=10.5, textColor=ink, leading=13, spaceAfter=2),
        "why": st("why", fontName="AR", fontSize=9, textColor=ink2, leading=12, spaceAfter=2),
        "tags": st("tags", fontName="AR", fontSize=7.5, textColor=muted, leading=10),
        "quiet": st("quiet", fontName="AR-I", fontSize=9, textColor=muted, leading=12),
        "rname": st("rname", fontName="AR-B", fontSize=10, textColor=ink, leading=13, spaceBefore=6, spaceAfter=1),
        "rbul": st("rbul", fontName="AR", fontSize=8.5, textColor=ink2, leading=11, spaceAfter=1, leftIndent=8, bulletIndent=0),
        "trupd": st("trupd", fontName="AR-B", fontSize=7.5, textColor=muted, leading=10, spaceAfter=4),
        "trh": st("trh", fontName="AR-B", fontSize=10, textColor=ink, leading=13, spaceBefore=6, spaceAfter=1),
        "trp": st("trp", fontName="AR", fontSize=9, textColor=ink2, leading=12, spaceAfter=2),
        "foot": st("foot", fontName="AR", fontSize=7.5, textColor=muted, leading=10, spaceBefore=2),
    }

    class Rule(Flowable):
        def __init__(self, w, color=line, thick=0.6):
            super().__init__(); self.w=w; self.color=color; self.thick=thick
        def wrap(self, aw, ah): self.width=aw; return (aw, self.thick+3)
        def draw(self):
            self.canv.setStrokeColor(self.color); self.canv.setLineWidth(self.thick)
            self.canv.line(0, 1, self.width, 1)

    story = []
    story.append(Paragraph("АО «РОСХИМ» · ДЕПАРТАМЕНТ МЕЖДУНАРОДНОГО РАЗВИТИЯ", S["eyebrow"]))
    story.append(Paragraph(esc(f'{DOC_TITLE} за {d.get("date_dmy","")}'), S["title"]))
    story.append(Paragraph(esc("Ежедневная сводка событий, влияющих на спрос, предложение и цены в химотрасли — "
                               "по продуктовому периметру компании и закреплённым регионам."), S["subtitle"]))
    story.append(Spacer(1, 5))

    # тикер как таблица плиток
    tk = d.get("ticker", [])
    if tk:
        cells = []
        for t in tk:
            dircol = {"up": colors.HexColor(C["hi"]), "down": colors.HexColor(C["accent"])}.get(t.get("dir"), muted)
            k = Paragraph(esc(t["k"]).upper(), st("tkk", fontName="AR", fontSize=6.5, textColor=muted, leading=8))
            v = Paragraph(f'<font color="#{dircol.hexval()[2:]}">{esc(t["v"])}</font>'
                          + (f' <font size="6" color="#{muted.hexval()[2:]}">{esc(t.get("d",""))}</font>' if t.get("d") else ""),
                          st("tkv", fontName="AR-B", fontSize=8.5, leading=11))
            cells.append([k, v])
        # разложить по строкам максимум 3 плитки
        rows = []
        per = 3
        flat = [Table([[c[0]],[c[1]]], style=TableStyle([("TOPPADDING",(0,0),(-1,-1),1),("BOTTOMPADDING",(0,0),(-1,-1),1),("LEFTPADDING",(0,0),(-1,-1),6),("RIGHTPADDING",(0,0),(-1,-1),6)])) for c in cells]
        for i in range(0, len(flat), per):
            row = flat[i:i+per]
            while len(row) < per: row.append("")
            rows.append(row)
        tkt = Table(rows, colWidths=[None]*per)
        tkt.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(-1,-1), colors.HexColor(C["surface2"])),
            ("BOX",(0,0),(-1,-1),0.5, line), ("INNERGRID",(0,0),(-1,-1),0.5, line),
            ("VALIGN",(0,0),(-1,-1),"TOP"), ("TOPPADDING",(0,0),(-1,-1),6),
            ("BOTTOMPADDING",(0,0),(-1,-1),6),
        ]))
        story.append(tkt)
    story.append(Spacer(1, 4))

    def item_flow(it, avail):
        imp = it.get("impact", "watch")
        col = colors.HexColor(IMPACT_COLOR.get(imp, C["watch"]))
        chips = [f'<font color="#{col.hexval()[2:]}"><b>{esc(it.get("impact_label","")).upper()}</b></font>']
        if it.get("update"):
            chips.append(f'<font color="#{accent_ink.hexval()[2:]}">ОБНОВЛЕНИЕ</font>')
        for dd in it.get("dirs", []):
            chips.append(f'<font color="#{ink2.hexval()[2:]}">{esc(dd)}</font>')
        if it.get("age"):
            chips.append(f'<font color="#{muted.hexval()[2:]}">{esc(it["age"])}</font>')
        meta_p = Paragraph(" &nbsp;·&nbsp; ".join(chips), S["metaline"])
        title_p = Paragraph(esc(it.get("title","")), S["itemtitle"])
        why_p = Paragraph(f'<font color="#{accent.hexval()[2:]}"><b>Почему важно:</b></font> '
                          + esc(it.get("why","")), S["why"])
        tagbits = list(it.get("regions", [])) + list(it.get("products", []))
        tagline = " · ".join(esc(x) for x in tagbits)
        if it.get("source_url"):
            tagline += (f'&nbsp;&nbsp;<a href="{urlesc(it["source_url"])}">'
                        f'<font color="#{accent_ink.hexval()[2:]}">{esc(it.get("source_name","Источник"))} →</font></a>')
        tags_p = Paragraph(tagline, S["tags"])
        content = [meta_p, title_p, why_p, tags_p]
        t = Table([[ "", content ]], colWidths=[3, avail-3])
        t.setStyle(TableStyle([
            ("BACKGROUND",(0,0),(0,-1), col),
            ("LEFTPADDING",(1,0),(1,-1),9), ("RIGHTPADDING",(1,0),(1,-1),4),
            ("TOPPADDING",(0,0),(-1,-1),5), ("BOTTOMPADDING",(0,0),(-1,-1),6),
            ("LEFTPADDING",(0,0),(0,-1),0), ("RIGHTPADDING",(0,0),(0,-1),0),
            ("VALIGN",(0,0),(-1,-1),"TOP"),
            ("LINEBELOW",(1,0),(1,-1),0.4, line),
        ]))
        return t

    AVAIL = A4[0] - 32*mm
    n_news = len(d["sections"])

    def section_header(num, title):
        return [Paragraph(f'<font color="#{accent.hexval()[2:]}">{num}</font>&nbsp;&nbsp;{esc(title)}', S["sec"]),
                Rule(AVAIL)]

    for idx, sec in enumerate(d["sections"], 1):
        story += section_header(str(idx).zfill(2), sec.get("title",""))
        if sec.get("note"):
            story.append(Paragraph(esc(sec["note"]), S["note"]))
        if sec.get("items"):
            for it in sec["items"]:
                story.append(KeepTogether(item_flow(it, AVAIL)))
                story.append(Spacer(1, 5))
        else:
            story.append(Paragraph("Существенных событий за сутки нет.", S["quiet"]))
            story.append(Spacer(1, 4))

    # регионы
    story += section_header(str(n_news+1).zfill(2), "Новости по регионам")
    for rc in d.get("regions_cards", []):
        story.append(Paragraph(f'{esc(rc["name"])} <font size="7" color="#{muted.hexval()[2:]}">— {esc(rc.get("role",""))}</font>', S["rname"]))
        for b in rc.get("bullets", []):
            txt = esc(b["text"])
            if b.get("url"):
                txt += f' <a href="{urlesc(b["url"])}"><font color="#{accent_ink.hexval()[2:]}">источник →</font></a>'
            story.append(Paragraph("•&nbsp; " + txt, S["rbul"]))

    # тренды
    tr = d.get("trends", {})
    if tr.get("points"):
        story += section_header(str(n_news+2).zfill(2), "Тренды мировой химотрасли за месяц")
        if tr.get("updated"):
            story.append(Paragraph(esc(tr["updated"]).upper(), S["trupd"]))
        for p in tr["points"]:
            story.append(Paragraph(esc(p["title"]), S["trh"]))
            story.append(Paragraph(esc(p["text"]), S["trp"]))

    story.append(Spacer(1, 8))
    story.append(Rule(AVAIL))
    story.append(Paragraph("<b>Методология.</b> Автосбор по открытым источникам, ранжирование по влиянию на спрос/предложение/цену; "
                           "приоритет — периметр АО «Росхим». Окно свежести 24–72 ч. "
                           "<b>Ограничения:</b> премиум-данные и «живые» котировки за пейволом недоступны — для сделок сверяйтесь с первичными источниками.", S["foot"]))

    def furniture(canv, doc_):
        canv.saveState()
        canv.setFont("AR", 7)
        canv.setFillColor(muted)
        canv.drawString(16*mm, 10*mm, f"АО «Росхим» · {DOC_TITLE} · для внутреннего пользования")
        canv.drawRightString(A4[0]-16*mm, 10*mm, f"{d.get('date_dmy','')}  ·  стр. {doc_.page}")
        canv.setStrokeColor(line); canv.setLineWidth(0.5)
        canv.line(16*mm, 13*mm, A4[0]-16*mm, 13*mm)
        draw_logo(canv, A4[0]-8*mm, A4[1]-6*mm, 12*mm)
        canv.restoreState()

    doc = SimpleDocTemplate(path, pagesize=A4,
                            leftMargin=16*mm, rightMargin=16*mm,
                            topMargin=19*mm, bottomMargin=16*mm,
                            title=f"{DOC_TITLE} за {d.get('date_dmy','')} — АО «Росхим»",
                            author="АО «Росхим»")
    doc.build(story, onFirstPage=furniture, onLaterPages=furniture)


# =========================================================================
#  ПОЧТА
# =========================================================================
def send_email(pdf_path, ref):
    """Отправить готовый PDF по почте (Gmail SMTP). Без переменных окружения — тихо пропускается,
    чтобы обычный локальный прогон build.py (без почтового конфига) не ломался."""
    addr = os.environ.get("GMAIL_ADDRESS")
    app_pw = os.environ.get("GMAIL_APP_PASSWORD")
    recipients_raw = os.environ.get("DIGEST_RECIPIENTS")
    if not (addr and app_pw and recipients_raw):
        print("⚠ Почта не отправлена: не заданы GMAIL_ADDRESS / GMAIL_APP_PASSWORD / DIGEST_RECIPIENTS")
        return
    recipients = [r.strip() for r in recipients_raw.split(",") if r.strip()]

    msg = EmailMessage()
    msg["Subject"] = f"Дайджест ключевых новостей за {dmy(ref)}"
    msg["From"] = addr
    msg["To"] = ", ".join(recipients)
    msg.set_content("Дайджест ключевых новостей химотрасли — во вложении (PDF).")

    with open(pdf_path, "rb") as f:
        msg.add_attachment(f.read(), maintype="application", subtype="pdf",
                            filename=os.path.basename(pdf_path))

    ctx = ssl.create_default_context()
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ctx) as s:
        s.login(addr, app_pw)
        s.send_message(msg)
    print(f"Письмо отправлено: {', '.join(recipients)}")


# =========================================================================
#  MAIN
# =========================================================================
def main():
    ref = date.today()
    issue_path = DEFAULT_ISSUE
    dry = False
    for a in sys.argv[1:]:
        if a.startswith("--asof="):          # пересборка выпуска задним числом
            ref = date.fromisoformat(a.split("=", 1)[1])
        elif a == "--dry-run":               # только проверка, без записи файлов
            dry = True
        else:
            issue_path = a

    with open(issue_path, "r", encoding="utf-8") as f:
        d = json.load(f)
    # тренды всегда берутся из trends.json (единственный источник правды),
    # копия внутри issue.json игнорируется — чтобы блок не разъезжался
    tp = os.path.join(RADAR_DIR, "trends.json")
    if os.path.exists(tp):
        with open(tp, "r", encoding="utf-8") as f:
            d["trends"] = json.load(f)

    # 1) структуру выпуска задаёт код
    warns = normalize_issue(d, ref)
    # 2) свежесть, источники, антиповтор
    hist_urls, hist_titles = load_history(ref)
    dropped = enforce_freshness(d, ref, hist_urls, hist_titles)

    print(f"Выпуск за {d['date_iso']} ({d['date_human']})")
    if dropped:
        print(f"⚠ Отброшено — {len(dropped)}:")
        for where, what, why in dropped:
            print(f"   · [{where}] {what} — {why}")
    else:
        print("✓ Все пункты прошли контроль")
    for w in warns:
        print(f"⚠ {w}")

    total = sum(len(s["items"]) for s in d["sections"])
    empty = [s["id"] for s in d["sections"] if not s["items"]]
    print("Рубрики: " + ", ".join(f"{s['id']}={len(s['items'])}" for s in d["sections"])
          + f"  ·  всего {total}")
    if empty:
        print("⚠ Пустые рубрики: " + ", ".join(empty))
    thin = [rc["name"] for rc in d["regions_cards"]
            if rc["name"] in PINNED_REGIONS and not rc["bullets"]]
    if thin:
        print("⚠ Закреплённые регионы без пунктов: " + ", ".join(thin))

    if total == 0:
        print("✗ В выпуске не осталось ни одного пункта — файлы НЕ перезаписаны.\n"
              "  Доищи свежие события (даты проверять у источника) и собери issue.json заново.")
        sys.exit(1)
    if total < MIN_ITEMS_WARN:
        print(f"⚠ Пунктов всего {total} — выпуск жидковат, стоит доискать свежее.")
    if dry:
        print("(--dry-run: файлы не записаны)")
        return

    os.makedirs(OUT_DIR, exist_ok=True)
    stem = f"{FILE_PREFIX}-{ref.day:02d}-{ref.month:02d}-{ref.year}"
    html_path = os.path.join(OUT_DIR, stem + ".html")
    pdf_path = os.path.join(OUT_DIR, stem + ".pdf")

    with open(html_path, "w", encoding="utf-8") as f:
        f.write(render_html(d))
    shutil.copyfile(html_path, os.path.join(OUT_DIR, f"{FILE_PREFIX}-latest.html"))

    build_pdf(d, pdf_path)
    shutil.copyfile(pdf_path, os.path.join(OUT_DIR, f"{FILE_PREFIX}-latest.pdf"))

    added = append_history(d, ref)   # журнал ведёт код — только по вошедшему в выпуск

    print("HTML:", html_path)
    print("PDF :", pdf_path)
    print("LATEST:", os.path.join(OUT_DIR, f"{FILE_PREFIX}-latest.pdf"))
    print(f"Журнал: +{added} строк в history.jsonl")

    send_email(pdf_path, ref)


if __name__ == "__main__":
    main()
