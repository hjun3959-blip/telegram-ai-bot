---
name: Bazi 八字排盘
description: How the four-pillar (八字) chart must be computed, and the lunar_python gotchas behind it.
---

# 八字排盘 (four-pillar / bazi) calculation

The chart lives in `services/mingli_service.py` (`compute_bazi`), driven by the FSM in `routers/mingli.py`.

## Rule: use a solar-term-aware engine, never hand-rolled date math
八字排盘 cannot be derived from the Gregorian month or a hand-written Julian-Day formula:
- 年柱 changes at **立春** (~Feb 4), not Jan 1 and not 农历正月初一.
- 月柱 changes at the **节 of each 节气**, not at the civil month boundary.
- 日柱 needs a correct continuous day count; 时柱 depends on the (correct) day stem and the 子时 rollover.

**Why:** the original implementation used `year - 1864` for the year, a static Gregorian-month→branch table for the month, and a buggy JDN formula (`m = month + 12*a - 2`, missing the +4800 epoch) for the day. Result: day pillar wrong in 100% of cases, year/month pillars wrong near every boundary.

**How to apply:** compute via `lunar_python` — `Solar.fromYmdHms(...).getLunar().getEightChar()` — which is the寿星天文历-grade reference and handles 节气/立春/子时 correctly. Keep the return dict backward-compatible (`pillars`/`wuxing`/`day_master`/`day_master_element`).

## Gotcha: lunar_python silently accepts invalid Gregorian dates
`Solar.fromYmdHms(1995, 2, 30, ...)` does **not** raise — it returns a confident but meaningless chart (also Feb 29 in non-leap years, Apr 31, etc.).
**How to apply:** pre-validate with `datetime.date(year, month, day)` (raises `ValueError`) before排盘, and validate the full date in the FSM `ask_day` handler (it only checks day∈1–31, which misses month/leap-year cross-checks).

## Convention: 晚子时 day pillar
Call `eight_char.setSect(2)` explicitly so 23:00–24:00 maps the day pillar to the **current** day deterministically across lunar_python versions (sect 2 is the default but pin it).

## Verifying changes
Compare against `lunar_python`'s own EightChar across edge dates (立春 day, pre-节气 dates, 23:00 子时). Note: comparing `compute_bazi` to lunar_python is circular once `compute_bazi` *uses* lunar_python — also spot-check a couple of independently-known charts (e.g. 2000-01-01 → 己卯/丙子/戊午/壬子).
