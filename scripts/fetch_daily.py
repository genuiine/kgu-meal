"""
급식형 식당(매일 식단이 바뀌는 곳) 식단 수집기 → daily.json
  - 감성코어(제3복지관): 경기대 공식 '수원캠퍼스 식당' 달력 페이지의 주간 PDF 표를 pdfplumber로 파싱
  - 경기드림타워(생활관 식당): dorm.kyonggi.ac.kr '금주의 식단' HTML 표 파싱 (이번 주 + 다음 주)

GitHub Actions(.github/workflows/daily-menu.yml)가 매일 실행하고 내용이 바뀐 경우에만 커밋한다.
로컬 실행: uv run --with pdfplumber --with requests python scripts/fetch_daily.py

daily.json 구조:
{
  "generatedAt": "...",
  "cafes": {
    "gamsung": {"name","source","meals":[{key,label,time}],"days":{"YYYY-MM-DD":{"lunch":[...]}},"weeks":[{start,end,pdf,note,error?}]},
    "dorm":    {"name","source","meals":[...],"days":{...},"hours":{breakfast,lunch,dinner},"weeks":[{start,end,error?}]}
  }
}
"""
from __future__ import annotations

import html as htmlmod
import io
import json
import re
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import pdfplumber
import requests

OUT = Path(__file__).resolve().parent.parent / "daily.json"
UA = {"User-Agent": "Mozilla/5.0 (kgumeal.com daily-menu fetcher; +https://kgumeal.com)"}
KST = timezone(timedelta(hours=9))

KGU_BASE = "https://www.kyonggi.ac.kr"
GAMSUNG_PAGE = KGU_BASE + "/www/selectTnRstrntMenuListU.do?key=7138&sc1=30"
DORM_PAGE = "https://dorm.kyonggi.ac.kr:446/Khostel/mall_main.php?viewform=B0001_foodboard_list"

MEAL_KEYS = {
    "아침": "breakfast", "조식": "breakfast",
    "점심": "lunch", "중식": "lunch",
    "저녁": "dinner", "석식": "dinner",
}


def get(url: str, **kw) -> requests.Response:
    last = None
    for _ in range(3):
        try:
            r = requests.get(url, headers=UA, timeout=30, **kw)
            r.raise_for_status()
            return r
        except Exception as e:  # noqa: BLE001
            last = e
    raise RuntimeError(f"GET 실패 {url}: {last}")


def clean(s: str | None) -> str:
    return re.sub(r"\s+", " ", htmlmod.unescape(s or "")).strip()


def strip_tags(s: str) -> str:
    return re.sub(r"<[^>]+>", "", s)


# ───────────────────────── 감성코어 (주간 PDF) ─────────────────────────
LINK_RE = re.compile(
    r'<a[^>]*class="[^"]*btn_menu[^"]*"[^>]*data-pdf="([^"]+)"[^>]*data-start="(\d{4}-\d{2}-\d{2})"[^>]*data-end="(\d{4}-\d{2}-\d{2})"',
    re.S,
)
HEAD_RE = re.compile(r"(\d{1,2})\s*/\s*(\d{1,2})")  # 09/14일(월)
TIME_RE = re.compile(r"(\d{1,2}:\d{2})")


def month_str(d: date) -> str:
    return f"{d.year}-{d.month:02d}"


def gamsung_weeks(today: date) -> list[dict]:
    """이번 달 + 다음 달 달력에서 주간 PDF 링크 수집 (pdf 경로 기준 중복 제거)."""
    seen: dict[str, dict] = {}
    nxt = (today.replace(day=1) + timedelta(days=32)).replace(day=1)
    for m in (month_str(today), month_str(nxt)):
        try:
            page = get(GAMSUNG_PAGE + "&sc2=" + m).text
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 감성코어 {m} 페이지 실패: {e}", file=sys.stderr)
            continue
        for pdf, start, end in LINK_RE.findall(page):
            seen.setdefault(pdf, {"pdf": KGU_BASE + pdf if pdf.startswith("/") else pdf, "start": start, "end": end})
    weeks = sorted(seen.values(), key=lambda w: w["start"])
    lo = (today - timedelta(days=today.weekday() + 7)).isoformat()   # 지난주 월요일
    hi = (today + timedelta(days=21)).isoformat()
    return [w for w in weeks if w["end"] >= lo and w["start"] <= hi]


def resolve_date(start: str, mm: int, dd: int) -> str:
    y, sm = int(start[:4]), int(start[5:7])
    if mm < sm - 6:  # 12월 시작 주가 1월로 넘어가는 경우
        y += 1
    return f"{y}-{mm:02d}-{dd:02d}"


def parse_gamsung_pdf(pdf_bytes: bytes, week: dict, meals: list[dict], days: dict) -> dict:
    note = ""
    found = 0
    with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
        for page in pdf.pages:
            text = page.extract_text() or ""
            m = re.search(r"\*([^*\n]+)\*", text)
            if m and not note:
                note = clean(m.group(1))
            for table in page.extract_tables():
                if not table or len(table) < 2:
                    continue
                cols: dict[int, str] = {}
                for i, h in enumerate(table[0]):
                    hm = HEAD_RE.search(clean(h))
                    if hm:
                        cols[i] = resolve_date(week["start"], int(hm.group(1)), int(hm.group(2)))
                if not cols:
                    continue
                for row in table[1:]:
                    label_raw = row[0] or ""
                    label = clean(label_raw.split("\n")[0]) or "식단"
                    times = TIME_RE.findall(label_raw)
                    key = MEAL_KEYS.get(label, "meal" + str(len(meals) + 1))
                    if not any(x["key"] == key for x in meals):
                        meals.append({"key": key, "label": label, "time": f"{times[0]}~{times[-1]}" if len(times) >= 2 else ""})
                    for i, iso in cols.items():
                        items = [clean(x) for x in (row[i] if i < len(row) else "" or "").split("\n") if clean(x)]
                        if items:
                            days.setdefault(iso, {})[key] = items
                            found += 1
    out = {**week, "note": note}
    if not found:
        out["error"] = "표 파싱 실패"
    return out


def fetch_gamsung(today: date) -> dict:
    meals: list[dict] = []
    days: dict[str, dict] = {}
    weeks_out = []
    for w in gamsung_weeks(today):
        try:
            weeks_out.append(parse_gamsung_pdf(get(w["pdf"]).content, w, meals, days))
            print(f"[ok] 감성코어 {w['start']}~{w['end']}")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 감성코어 PDF 실패 {w['start']}: {e}", file=sys.stderr)
            weeks_out.append({**w, "note": "", "error": str(e)[:200]})
    return {"name": "감성코어", "source": GAMSUNG_PAGE, "meals": meals, "days": dict(sorted(days.items())), "weeks": weeks_out}


# ───────────────────────── 경기드림타워 (생활관 HTML) ─────────────────────────
DORM_ROW_RE = re.compile(r"<tr>\s*<th>(.*?)</th>(.*?)</tr>", re.S)
DORM_DATE_RE = re.compile(r"(\d{4}-\d{2}-\d{2})")
DORM_TD_RE = re.compile(r"<td[^>]*>(.*?)</td>", re.S)
DORM_HOURS_RE = re.compile(r"<t[dh][^>]*>\s*(조식|중식|석식)\s*</t[dh]>\s*<td[^>]*>(.*?)</td>\s*<td[^>]*>(.*?)</td>", re.S)


def dorm_page(gday: date | None) -> str:
    url = DORM_PAGE if gday is None else f"{DORM_PAGE}&gyear={gday.year}&gmonth={gday.month:02d}&gday={gday.day:02d}"
    r = get(url, verify=True)
    r.encoding = "euc-kr"
    return r.text


def parse_dorm(page: str, days: dict, hours: dict) -> tuple[str | None, str | None]:
    """식단 표 파싱 → days 갱신. 반환: (주 시작일, 주 종료일)"""
    for meal, semester, weekend in DORM_HOURS_RE.findall(page):
        key = MEAL_KEYS[meal]
        hours.setdefault(key, {"semester": clean(strip_tags(semester)), "weekend": clean(strip_tags(weekend))})
    first = last = None
    body = page.split("<tbody>", 1)[-1].split("</tbody>", 1)[0]
    for th, rest in DORM_ROW_RE.findall(body):
        dm = DORM_DATE_RE.search(th)
        if not dm:
            continue
        iso = dm.group(1)
        first = first or iso
        last = iso
        tds = DORM_TD_RE.findall(rest)
        for key, cell in zip(("breakfast", "lunch", "dinner"), tds):
            items = [clean(strip_tags(x)) for x in re.split(r"<br\s*/?>", cell)]
            items = [x for x in items if x]
            if items:
                days.setdefault(iso, {})[key] = items
    return first, last


def fetch_dorm(today: date) -> dict:
    days: dict[str, dict] = {}
    hours: dict[str, dict] = {}
    weeks_out = []
    next_sunday = today + timedelta(days=7 - (today.weekday() + 1) % 7)  # 다음 주 일요일(표 시작일)
    for gday in (None, next_sunday):
        try:
            first, last = parse_dorm(dorm_page(gday), days, hours)
            weeks_out.append({"start": first, "end": last})
            print(f"[ok] 드림타워 {first}~{last}")
        except Exception as e:  # noqa: BLE001
            print(f"[warn] 드림타워 실패 ({gday}): {e}", file=sys.stderr)
            weeks_out.append({"start": None if gday is None else gday.isoformat(), "end": None, "error": str(e)[:200]})
    meals = []
    for key, label in (("breakfast", "아침"), ("lunch", "점심"), ("dinner", "저녁")):
        h = hours.get(key, {}).get("semester", "")
        meals.append({"key": key, "label": label, "time": "" if (not h or "미운영" in h) else h.replace(" ", "")})
    return {"name": "경기드림타워", "source": DORM_PAGE, "meals": meals, "hours": hours,
            "days": dict(sorted(days.items())), "weeks": weeks_out}


# ───────────────────────── main ─────────────────────────
def main() -> int:
    today = datetime.now(KST).date()
    cafes = {"gamsung": fetch_gamsung(today), "dorm": fetch_dorm(today)}
    for k, c in cafes.items():
        print(f"[info] {k}: {len(c['days'])}일 / meals={[m['key'] for m in c['meals']]}")

    payload = {"generatedAt": datetime.now(KST).isoformat(timespec="seconds"), "cafes": cafes}

    # 새 결과가 완전히 비었으면(사이트 장애 등) 기존 파일을 보존한다
    if OUT.exists():
        try:
            old = json.loads(OUT.read_text("utf-8"))
            if old.get("cafes") == cafes:
                print("[info] 변경 없음")
                return 0
            if not any(c["days"] for c in cafes.values()) and any(c.get("days") for c in old.get("cafes", {}).values()):
                print("[warn] 새 데이터가 전부 비어 있어 기존 파일 유지", file=sys.stderr)
                return 0
        except Exception:  # noqa: BLE001
            pass
    OUT.write_text(json.dumps(payload, ensure_ascii=False, indent=1) + "\n", "utf-8")
    print(f"[info] 저장: {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
