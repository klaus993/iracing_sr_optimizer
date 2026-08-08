#!/usr/bin/env python3
"""Standalone tool: the *meta* car in an iRacing series/class for a week.

Where `car_usage.py` answers "which car is most *used*", this answers "which car is
actually *good*" — combining usage share with win rate, average finish, and raw
qualifying/race pace, and letting you slice by the split you'd actually race in.

Three scopes (any combination, via --scope):
  all   every split of the week (the whole field)                     [default]
  top   only split 1 (the highest-SoF split per race slot — aliens)
  mine  only the split your --irating would land in, each race slot

For each car in the class it reports: entries + usage %, wins + win %, podium %,
average finish-in-class, and pace — the fastest QUALIFYING lap (clean low-fuel) plus a
robust 5th-percentile qual lap and the gap to the fastest car. Lap times from the API
are in ten-thousandths of a second.

This script reuses car_usage.py for auth, caching, and series/season resolution, so it
needs the same OAuth credentials in the environment (see car_usage.py --help).

Usage:
    python car_meta.py --series-id 447 --class IMSA23 --week 5 --scope all top
    python car_meta.py --series-id 539 --class IMSA23 --week 5 --irating 2652 --scope mine
    python car_meta.py --series-id 447 --class IMSA23 --week 5 --irating 2652 \
        --scope all top mine --csv > meta.csv

Run `python car_meta.py --help` for all options.
"""
from __future__ import annotations

import argparse
import csv
import io
import json
import logging
import statistics
import sys
import time
from collections import defaultdict
from typing import Optional

import car_usage as cu

logger = logging.getLogger("car_meta")


# --- Row-level helpers --------------------------------------------------------

def _fmt_lap(tenk: Optional[float]) -> str:
    """Ten-thousandths of a second -> 'm:ss.mmm' (or '—' for missing/invalid)."""
    if not tenk or tenk <= 0:
        return "—"
    s = tenk / 10000.0
    m = int(s // 60)
    return f"{m}:{s - 60 * m:06.3f}"


def _lap_seconds(tenk: Optional[int]) -> Optional[float]:
    """Ten-thousandths of a second -> seconds (float), or None."""
    if not tenk or tenk <= 0:
        return None
    return round(tenk / 10000.0, 3)


def _iter_sim_rows(result: dict, want: str):
    """Yield result rows from the simsession named/typed `want` ('RACE' or 'QUALIFY')."""
    want = want.strip().upper()
    for ss in result.get("session_results") or []:
        name = str(ss.get("simsession_name", "")).strip().upper()
        tname = str(ss.get("simsession_type_name", "")).strip().upper()
        if want in (name, tname):
            for row in ss.get("results", []) or []:
                yield row


def _row_irating(row: dict) -> Optional[int]:
    """Pre-race iRating for a result row (fall back to first driver_result for teams)."""
    v = row.get("oldi_rating")
    if isinstance(v, int) and v > 0:
        return v
    for dr in row.get("driver_results", []) or []:
        v = dr.get("oldi_rating")
        if isinstance(v, int) and v > 0:
            return v
    return None


def _percentile(sorted_vals: list[int], q: float) -> Optional[int]:
    """Nearest-rank percentile (q in [0,1]) of a pre-sorted list; None if empty."""
    if not sorted_vals:
        return None
    idx = int(len(sorted_vals) * q)
    return sorted_vals[max(0, min(len(sorted_vals) - 1, idx))]


# --- Split selection (pure) ---------------------------------------------------

def group_by_slot(results: list[dict]) -> dict:
    """Group subsession results by start_time (one race 'slot' = one wave of splits)."""
    slots: dict = defaultdict(list)
    for res in results:
        slots[res.get("start_time")].append(res)
    return slots


def select_top_split(results: list[dict], slots_per_wave: int = 1) -> list[dict]:
    """Keep the `slots_per_wave` highest-SoF splits within each start-time slot."""
    keep = []
    for group in group_by_slot(results).values():
        ordered = sorted(group, key=lambda r: r.get("event_strength_of_field", 0),
                         reverse=True)
        keep.extend(ordered[:slots_per_wave])
    return keep


def _split_irating_band(result: dict) -> Optional[tuple[int, int, float]]:
    """(min, max, median) pre-race iRating over a split's race entrants, or None."""
    irs = [ir for r in _iter_sim_rows(result, "RACE") if (ir := _row_irating(r))]
    if not irs:
        return None
    return min(irs), max(irs), statistics.median(irs)


def select_by_tier(results: list[dict]) -> list[tuple[int, list[dict], dict]]:
    """Partition every split by its rank-within-slot (1=top) into iRating tiers.

    Covers *all* iRatings at once: tier 1 is every slot's split 1 (fastest drivers),
    tier 2 every slot's split 2, and so on. Returns a list of (tier_rank, results, info)
    sorted by tier, where info carries the tier's SoF and iRating-band stats so a driver
    can map their iRating onto a tier. Tiers with no data are omitted.
    """
    tiers: dict[int, list[dict]] = defaultdict(list)
    for group in group_by_slot(results).values():
        ordered = sorted(group, key=lambda r: r.get("event_strength_of_field", 0),
                         reverse=True)
        for idx, res in enumerate(ordered, 1):
            tiers[idx].append(res)

    out = []
    for rank in sorted(tiers):
        subset = tiers[rank]
        sofs = [r.get("event_strength_of_field", 0) for r in subset]
        bands = [b for r in subset if (b := _split_irating_band(r)) is not None]
        info = {
            "n_splits": len(subset),
            "sof_min": min(sofs) if sofs else None,
            "sof_median": int(statistics.median(sofs)) if sofs else None,
            "sof_max": max(sofs) if sofs else None,
            "band_lo": min(b[0] for b in bands) if bands else None,
            "band_hi": max(b[1] for b in bands) if bands else None,
            "band_med": int(statistics.median([b[2] for b in bands])) if bands else None,
        }
        out.append((rank, subset, info))
    return out


def select_my_split(results: list[dict], irating: int) -> tuple[list[dict], dict]:
    """Pick, per slot, the split whose iRating band best fits `irating`.

    iRacing sorts registrants by iRating into splits (split 1 = highest), so the band
    that contains `irating` is the split you'd be placed in. We choose the split whose
    [min,max] iRating band brackets `irating` (distance 0), breaking ties/gaps by the
    closest median. Returns (chosen_results, info) where info has SoF/rank/band stats.
    """
    chosen, sofs, ranks, bands = [], [], [], []
    for group in group_by_slot(results).values():
        scored = []
        for res in group:
            band = _split_irating_band(res)
            if band is None:
                continue
            lo, hi, med = band
            in_band = 0.0 if lo <= irating <= hi else float(min(abs(irating - lo),
                                                                abs(irating - hi)))
            scored.append((in_band, abs(irating - med), res, lo, hi))
        if not scored:
            continue
        scored.sort(key=lambda t: (t[0], t[1]))
        _, _, res, lo, hi = scored[0]
        chosen.append(res)
        sofs.append(res.get("event_strength_of_field", 0))
        bands.append((lo, hi))
        ordered = sorted(group, key=lambda r: r.get("event_strength_of_field", 0),
                         reverse=True)
        ranks.append(ordered.index(res) + 1)
    info = {
        "matched_slots": len(chosen),
        "total_slots": len(group_by_slot(results)),
        "sof_min": min(sofs) if sofs else None,
        "sof_median": int(statistics.median(sofs)) if sofs else None,
        "sof_max": max(sofs) if sofs else None,
        "split_ranks": sorted(ranks),
        "rank_median": int(statistics.median(ranks)) if ranks else None,
        "band_lo": int(statistics.median([b[0] for b in bands])) if bands else None,
        "band_hi": int(statistics.median([b[1] for b in bands])) if bands else None,
    }
    return chosen, info


# --- Metric computation (pure) ------------------------------------------------

def compute_class_metrics(
    results: list[dict], car_class: Optional[str]
) -> dict[str, dict]:
    """Per-car performance metrics grouped by class short name.

    Returns {class_short: {"n_splits": int, "rows": [row, ...]}} where each row has
    usage, win, podium, finish, incident and qual/race pace stats. If `car_class` is
    given, only that class is returned; otherwise every class seen is returned.
    """
    target = car_class.strip().lower() if car_class else None
    agg: dict[str, dict] = {}

    def bucket(cls: str) -> dict:
        return agg.setdefault(cls, {
            "n_splits": 0, "entries": defaultdict(int), "wins": defaultdict(int),
            "podiums": defaultdict(int), "finishes": defaultdict(list),
            "incidents": defaultdict(list), "qual": defaultdict(list),
            "race": defaultdict(list), "_seen_split": defaultdict(bool),
        })

    for result in results:
        cmap = cu._build_class_map(result)
        sid = result.get("subsession_id")
        # Race rows -> usage/win/finish/incident/race-pace.
        for r in _iter_sim_rows(result, "RACE"):
            cls = cu._row_class_short_name(r, cmap)
            if cls is None:
                continue
            if target is not None and cls.strip().lower() != target:
                continue
            b = bucket(cls)
            if not b["_seen_split"][sid]:
                b["_seen_split"][sid] = True
                b["n_splits"] += 1
            car = r.get("car_name") or f"car_id:{r.get('car_id')}"
            b["entries"][car] += 1
            fic = r.get("finish_position_in_class")
            if isinstance(fic, int) and fic >= 0:
                b["finishes"][car].append(fic + 1)
                if fic == 0:
                    b["wins"][car] += 1
                if fic <= 2:
                    b["podiums"][car] += 1
            inc = r.get("incidents")
            if isinstance(inc, int) and inc >= 0:
                b["incidents"][car].append(inc)
            bl = r.get("best_lap_time")
            if isinstance(bl, int) and bl > 0:
                b["race"][car].append(bl)
        # Qual rows -> clean pace.
        for r in _iter_sim_rows(result, "QUALIFY"):
            cls = cu._row_class_short_name(r, cmap)
            if cls is None:
                continue
            if target is not None and cls.strip().lower() != target:
                continue
            b = bucket(cls)
            car = r.get("car_name") or f"car_id:{r.get('car_id')}"
            bl = r.get("best_lap_time")
            if isinstance(bl, int) and bl > 0:
                b["qual"][car].append(bl)

    out: dict[str, dict] = {}
    for cls, b in agg.items():
        total = sum(b["entries"].values())
        n_splits = b["n_splits"]
        rows = []
        for car, ent in b["entries"].items():
            ql = sorted(b["qual"][car])
            fins = b["finishes"][car]
            incs = b["incidents"][car]
            rl = b["race"][car]
            rows.append({
                "car": car,
                "entries": ent,
                "usage_pct": round(ent / total * 100, 1) if total else 0.0,
                "wins": b["wins"][car],
                "win_pct_splits": round(b["wins"][car] / n_splits * 100, 1) if n_splits else 0.0,
                "win_per_entry": round(b["wins"][car] / ent * 100, 1) if ent else 0.0,
                "podium_pct": round(b["podiums"][car] / ent * 100, 1) if ent else 0.0,
                "avg_finish": round(statistics.mean(fins), 1) if fins else None,
                "avg_inc": round(statistics.mean(incs), 1) if incs else None,
                "best_qual": min(ql) if ql else None,
                "p5_qual": _percentile(ql, 0.05),
                "best_race": min(rl) if rl else None,
            })
        rows.sort(key=lambda d: d["entries"], reverse=True)
        out[cls] = {"n_splits": n_splits, "rows": rows}
    return out


# --- Rendering ----------------------------------------------------------------

def render_table(cls: str, data: dict, top: Optional[int]) -> str:
    rows = data["rows"][:top]
    fastest = min((r["best_qual"] for r in data["rows"] if r["best_qual"]), default=None)
    lines = [f"{cls} — {data['n_splits']} splits, {sum(r['entries'] for r in data['rows'])} entries"]
    hdr = (f"{'Car':<30}{'Ent':>5}{'Use%':>7}{'Win':>5}{'Win%':>7}{'Pod%':>7}"
           f"{'AvgFin':>8}{'BestQual':>11}{'p5Qual':>11}{'Gap':>8}")
    lines.append(hdr)
    lines.append("-" * len(hdr))
    for r in rows:
        gap = ""
        if r["best_qual"] and fastest:
            gap = "0.000" if r["best_qual"] == fastest else f"+{(r['best_qual']-fastest)/10000:.3f}"
        af = f"{r['avg_finish']:.1f}" if r["avg_finish"] is not None else "—"
        car_s = r["car"][:30]
        lines.append(
            f"{car_s:<30}{r['entries']:>5}{r['usage_pct']:>6.1f}%{r['wins']:>5}"
            f"{r['win_pct_splits']:>6.1f}%{r['podium_pct']:>6.1f}%{af:>8}"
            f"{_fmt_lap(r['best_qual']):>11}{_fmt_lap(r['p5_qual']):>11}{gap:>8}")
    return "\n".join(lines)


def scope_heading(scope: str, info: Optional[dict], irating: Optional[int]) -> str:
    if scope == "all":
        return "ALL SPLITS (whole field)"
    if scope == "top":
        return "TOP SPLIT ONLY (split 1 per race slot — the aliens)"
    if scope == "mine" and info:
        band = (f"~{info['band_lo']}–{info['band_hi']}"
                if info["band_lo"] is not None else "?")
        return (f"YOUR SPLIT (iRating {irating}, matched {info['matched_slots']}/"
                f"{info['total_slots']} slots | SoF median {info['sof_median']} "
                f"| split rank median {info['rank_median']} | band {band})")
    return scope.upper()


def tier_heading(rank: int, info: dict) -> str:
    band = (f"iRating ~{info['band_lo']}–{info['band_hi']} (median {info['band_med']})"
            if info["band_lo"] is not None else "iRating ?")
    return (f"TIER {rank} — split {rank} of each slot | {info['n_splits']} splits "
            f"| SoF median {info['sof_median']} | {band}")


# --- Machine-readable export --------------------------------------------------

EXPORT_FIELDS = [
    "scope", "series_id", "series_name", "season_year", "season_quarter", "week",
    "track", "car_class", "n_splits", "rank", "car", "entries", "usage_pct",
    "wins", "win_pct_splits", "win_per_entry", "podium_pct", "avg_finish",
    "avg_inc", "best_qual_s", "p5_qual_s", "best_race_s",
]


def build_export_rows(scope: str, by_class: dict, context: dict, top: Optional[int]) -> list[dict]:
    rows = []
    for cls, data in by_class.items():
        for rank, r in enumerate(data["rows"][:top], 1):
            rows.append({
                "scope": scope,
                **{k: context.get(k) for k in
                   ("series_id", "series_name", "season_year", "season_quarter", "week", "track")},
                "car_class": cls,
                "n_splits": data["n_splits"],
                "rank": rank,
                "car": r["car"],
                "entries": r["entries"],
                "usage_pct": r["usage_pct"],
                "wins": r["wins"],
                "win_pct_splits": r["win_pct_splits"],
                "win_per_entry": r["win_per_entry"],
                "podium_pct": r["podium_pct"],
                "avg_finish": r["avg_finish"],
                "avg_inc": r["avg_inc"],
                "best_qual_s": _lap_seconds(r["best_qual"]),
                "p5_qual_s": _lap_seconds(r["p5_qual"]),
                "best_race_s": _lap_seconds(r["best_race"]),
            })
    return rows


def export_csv(rows: list[dict]) -> str:
    out = io.StringIO()
    w = csv.DictWriter(out, fieldnames=EXPORT_FIELDS)
    w.writeheader()
    w.writerows(rows)
    return out.getvalue().rstrip("\r\n")


def export_json(rows: list[dict]) -> str:
    return json.dumps(rows, indent=2)


# --- CLI ----------------------------------------------------------------------

def parse_args(argv: Optional[list[str]] = None) -> argparse.Namespace:
    p = argparse.ArgumentParser(
        prog="car_meta",
        description="The meta car in an iRacing series/class for a week: usage + win "
                    "rate + pace, sliceable by top split or your own iRating split.",
    )
    p.add_argument("--series", default="IMSA",
                   help="Series name to match (exact wins, else substring). "
                        "Ignored if --series-id is given. Tip: 'IMSA' is ambiguous; "
                        "pass --series-id 447 (open) or 539 (fixed).")
    p.add_argument("--series-id", type=int, default=None,
                   help="Select a series by exact id (e.g. 447=IMSA open, 539=IMSA fixed)")
    p.add_argument("--class", dest="car_class", default=None,
                   help="Car class short name (e.g. IMSA23). Omit for every class.")
    p.add_argument("--week", type=int, default=None,
                   help="Race week, 1-indexed (default: current week)")
    p.add_argument("--year", type=int, default=None, help="Season year override")
    p.add_argument("--quarter", type=int, default=None, help="Season quarter override 1-4")
    p.add_argument("--scope", nargs="+", choices=["all", "top", "mine", "tiers"],
                   default=["all"],
                   help="Which split population(s) to analyze (default: all). "
                        "'mine' needs --irating (your split); 'tiers' breaks the meta "
                        "down across every iRating tier. Passing 'mine' without "
                        "--irating falls back to 'tiers'.")
    p.add_argument("--irating", type=int, default=None,
                   help="Your iRating, for --scope mine (the split you'd be placed in). "
                        "Omit it and 'mine' becomes 'tiers' (all iRatings).")
    p.add_argument("--slots", type=int, default=1,
                   help="For --scope top: how many highest-SoF splits per slot (default 1)")
    p.add_argument("--top", type=int, default=None, help="Show only the top N cars")
    p.add_argument("--max-sessions", type=int, default=None,
                   help="Cap subsessions fetched (quick sample)")
    fmt = p.add_mutually_exclusive_group()
    fmt.add_argument("--csv", dest="output_csv", action="store_true",
                     help="Output CSV to stdout (logs/progress stay on stderr)")
    fmt.add_argument("--json", dest="output_json", action="store_true",
                     help="Output JSON to stdout (logs/progress stay on stderr)")
    p.add_argument("--verbose", "-v", action="count", default=0,
                   help="Verbose logging: -v INFO, -vv DEBUG")
    return p.parse_args(argv)


def main(argv: Optional[list[str]] = None) -> int:
    args = parse_args(argv)
    level = logging.DEBUG if args.verbose >= 2 else logging.INFO if args.verbose == 1 else logging.WARNING
    logging.basicConfig(level=level,
                        format="%(asctime)s %(levelname)s [%(name)s] %(message)s",
                        datefmt="%H:%M:%S")
    logger.setLevel(level)
    for noisy in ("urllib3", "iracingdataapi", "requests", "botocore", "boto3"):
        logging.getLogger(noisy).setLevel(logging.WARNING)

    # 'mine' without an iRating means "show me every tier" — normalize it to 'tiers',
    # de-duplicating while preserving order.
    scopes = ["tiers" if (s == "mine" and args.irating is None) else s for s in args.scope]
    scopes = list(dict.fromkeys(scopes))

    start = time.monotonic()
    try:
        client = cu.get_client()
        series_id, series_name = cu.resolve_series(client, args.series, args.series_id)
        season_year, season_quarter, race_week_num, track_name = cu.resolve_season_and_week(
            client, series_id, args.week, args.year, args.quarter)
        print(f"Series: {series_name} (id {series_id}) | {season_year}s{season_quarter} "
              f"week {race_week_num + 1}"
              f"{f' — {track_name}' if track_name else ''} | class {args.car_class or 'ALL'}",
              file=sys.stderr)

        subsessions = cu.find_week_subsessions(
            client, series_id, season_year, season_quarter, race_week_num, cu.RACE_EVENT_TYPE)
        if args.max_sessions is not None:
            subsessions = subsessions[: args.max_sessions]
        print(f"Found {len(subsessions)} race subsessions.", file=sys.stderr)

        results = []
        for i, sid in enumerate(subsessions, 1):
            cached = (cu.RESULTS_CACHE_DIR / f"{sid}.json").exists()
            results.append(cu.fetch_result(client, sid))
            if i % 25 == 0 or i == len(subsessions):
                print(f"  ...processed {i}/{len(subsessions)}", file=sys.stderr)
            if not cached:
                time.sleep(cu.THROTTLE_SECONDS)
    except cu.IRacingAPIError as e:
        print(f"Error: {e}", file=sys.stderr)
        return 1
    except Exception as e:  # pragma: no cover - surfaced with -vv
        logger.debug("Unexpected error", exc_info=True)
        print(f"Unexpected error: {e}\nRe-run with -vv for a traceback.", file=sys.stderr)
        return 1

    if not track_name:
        track_name = cu._track_from_results(results)
    context = {
        "series_id": series_id, "series_name": series_name,
        "season_year": season_year, "season_quarter": season_quarter,
        "week": race_week_num + 1, "track": track_name,
    }

    def render_scope(scope_label: str, heading: str, subset: list[dict]) -> None:
        """Compute metrics for one split population and append human/export output."""
        by_class = compute_class_metrics(subset, args.car_class)
        if args.output_csv or args.output_json:
            export_rows.extend(build_export_rows(scope_label, by_class, context, args.top))
            return
        block = [f"\n{'='*len(heading)}\n{heading}\n{'='*len(heading)}"]
        if not by_class:
            block.append(f"(no entries for class {args.car_class!r} in this scope)")
        for cls, data in sorted(by_class.items(),
                                key=lambda kv: sum(r["entries"] for r in kv[1]["rows"]),
                                reverse=True):
            block.append(render_table(cls, data, args.top))
        human_blocks.append("\n".join(block))

    export_rows: list[dict] = []
    human_blocks: list[str] = []
    for scope in scopes:
        if scope == "tiers":
            for rank, subset, info in select_by_tier(results):
                render_scope(f"tier{rank}", tier_heading(rank, info), subset)
        else:
            if scope == "all":
                subset, info = results, None
            elif scope == "top":
                subset, info = select_top_split(results, args.slots), None
            else:  # mine
                subset, info = select_my_split(results, args.irating)
            render_scope(scope, scope_heading(scope, info, args.irating), subset)

    if args.output_csv:
        print(export_csv(export_rows))
    elif args.output_json:
        print(export_json(export_rows))
    else:
        print(f"\n{series_name} — {season_year}s{season_quarter} week {race_week_num + 1}"
              f"{f' — {track_name}' if track_name else ''}")
        print("\n".join(human_blocks))
    logger.info("Done in %.1fs", time.monotonic() - start)
    return 0


if __name__ == "__main__":
    sys.exit(main())
