#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Свод фактов по бренду в один JSON для страницы.

Факт — это одна строка «артикул × день»: заказы, выкупы, возвраты и все деньги
из финотчёта. Периоды, недели, месяцы и любые срезы страница считает сама из
этих строк, поэтому переключение периода не требует пересборки.
"""
import json
import os
from collections import defaultdict
from datetime import datetime, timezone, timedelta

HERE = os.path.dirname(os.path.abspath(__file__))
MSK = timezone(timedelta(hours=3))

SUPPLIER_RU = {"new": "новое", "confirm": "на сборке", "complete": "собрано",
               "cancel": "отменено продавцом", "cancel_client": "отменено покупателем",
               "declined_by_client": "отказ до сборки"}
WB_RU = {"waiting": "у продавца", "sorted": "отсортирован", "sold": "получен покупателем",
         "canceled": "отменён", "canceled_by_client": "отменён покупателем",
         "declined_by_client": "отказ покупателя", "defect": "брак",
         "ready_for_pickup": "в ПВЗ, ждёт покупателя", "canceled_by_bank": "отменён банком"}
CANCELLED = {"canceled", "canceled_by_client", "declined_by_client", "defect",
             "canceled_by_bank"}
CARGO = {1: "МГТ", 2: "СГТ", 3: "КГТ"}


def path(*p):
    return os.path.join(HERE, *p)


def load(name, default=None):
    try:
        with open(path(name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default if default is not None else {}


def parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def iso(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ") if dt else None


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


def day_of(ts):
    """Дата в МСК: WB отдаёт статистику уже в московском времени, финотчёт в UTC."""
    if not ts:
        return None
    s = str(ts)
    if len(s) == 10:
        return s
    d = parse(s)
    return d.astimezone(MSK).date().isoformat() if d else s[:10]


def supply_state(s):
    if not s:
        return "нет поставки", "none"
    if s.get("rejectDt"):
        return "отклонена", "bad"
    if s.get("scanDt"):
        return "принята WB", "done"
    if s.get("closedAt") or s.get("done"):
        return "отгружается", "way"
    return "черновик", "draft"


def stage_of(rec, sup):
    ss, ws = rec.get("supplierStatus"), rec.get("wbStatus")
    if ws in CANCELLED or ss in ("cancel", "cancel_client", "declined_by_client"):
        return "отменён", "cancel"
    if ws == "sold":
        return "выкуплен", "sold"
    if ws == "ready_for_pickup":
        return "ждёт в ПВЗ", "pickup"
    if sup and sup.get("scanDt"):
        return "принят WB", "accepted"
    if sup and (sup.get("closedAt") or sup.get("done")):
        return "в пути на склад WB", "way"
    if rec.get("supplyId"):
        return "в поставке", "supply"
    if ss == "confirm":
        return "на сборке", "assembly"
    return "в очереди сборки", "queue"


def main():
    cfg = load("config.json")
    cards = load("state/cards.json")
    orders = load("state/orders.json")
    sales = load("state/sales.json")
    finance = load("state/finance.json")
    fbs = load("state/fbs_orders.json")
    supplies = load("state/supplies.json")
    stocks = load("state/stocks.json")
    whs = load("state/warehouses.json")
    fbo = load("state/fbo_remains.json")
    cursor = load("state/cursor.json")
    now = datetime.now(timezone.utc)

    # ---------------------------------------------------------- справочники
    art_of = {}
    articles = []
    for nm, c in sorted(cards.items(), key=lambda x: (x[1].get("vendorCode") or "")):
        art_of[int(nm)] = c.get("vendorCode") or str(nm)
        articles.append({"nmId": int(nm), "article": c.get("vendorCode") or str(nm),
                         "subject": c.get("subject"), "title": c.get("title"),
                         "photo": c.get("photo"),
                         "sizes": sorted({z.get("techSize") for z in (c.get("sizes") or [])
                                          if z.get("techSize")})})
    sku_nm = {}
    for nm, c in cards.items():
        for z in c.get("sizes") or []:
            for s in z.get("skus") or []:
                sku_nm[str(s)] = int(nm)

    # -------------------------------------------------- факты «артикул × день»
    F = lambda: defaultdict(float)                                    # noqa: E731
    facts = defaultdict(F)

    def model_of(wt):
        return "fbs" if (wt or "").strip() == "Склад продавца" else "fbo"

    def cell(day, nm, model="na"):
        return facts[(day, int(nm or 0), model)]

    srid_model = {}

    # заказы
    sale_srids = {r.get("srid") for r in sales.values()
                  if str(r.get("saleID", "")).startswith("S")}
    for r in orders.values():
        nm = r.get("nmId")
        if nm not in art_of:
            continue
        model = model_of(r.get("warehouseType"))
        if r.get("srid"):
            srid_model[r["srid"]] = model
        c = cell(day_of(r.get("date")), nm, model)
        c["ordQty"] += 1
        c["ordSum"] += num(r.get("priceWithDisc"))
        if r.get("isCancel"):
            c["ordCancelQty"] += 1
        if r.get("srid") in sale_srids:
            c["ordBought"] += 1

    # продажи и возвраты
    for r in sales.values():
        nm = r.get("nmId")
        if nm not in art_of:
            continue
        model = model_of(r.get("warehouseType"))
        if r.get("srid"):
            srid_model.setdefault(r["srid"], model)
        c = cell(day_of(r.get("date")), nm, model)
        sid = str(r.get("saleID", ""))
        if sid.startswith("R"):
            c["retQty"] += 1
            c["retSum"] += num(r.get("priceWithDisc"))
        else:
            c["saleQty"] += 1
            c["saleSum"] += num(r.get("priceWithDisc"))
            c["saleFinished"] += num(r.get("finishedPrice"))
            c["forPayStat"] += num(r.get("forPay"))

    # финотчёт
    for r in finance.values():
        nm = r.get("_nmId") or r.get("nmId") or sku_nm.get(str(r.get("sku") or ""))
        if nm not in art_of:
            continue
        day = day_of(r.get("saleDt") or r.get("rrDate"))
        c = cell(day, nm, srid_model.get(r.get("srid"), "na"))
        doc = (r.get("docTypeName") or "").strip()
        oper = (r.get("sellerOperName") or "")
        qty = num(r.get("quantity"))
        seller = num(r.get("retailPriceWithDisc")) * (qty or 1)
        if doc == "Продажа":
            c["finSaleQty"] += qty
            c["sellerPrice"] += seller          # цена продавца до скидки маркетплейса
            c["retail"] += num(r.get("retailAmount"))
            c["forPay"] += num(r.get("forPay"))
            c["acquiring"] += num(r.get("acquiringFee"))
            c["ppvz"] += num(r.get("ppvzReward"))
        elif doc == "Возврат":
            c["finRetQty"] += qty
            c["sellerPrice"] -= seller
            c["retail"] -= num(r.get("retailAmount"))
            c["forPay"] -= num(r.get("forPay"))
            c["acquiring"] -= num(r.get("acquiringFee"))
            c["ppvz"] -= num(r.get("ppvzReward"))
        if "Возмещение издержек" in oper or num(r.get("rebillLogisticCost")):
            c["logistics"] += num(r.get("rebillLogisticCost"))
        c["storage"] += num(r.get("paidStorage"))
        c["acceptance"] += num(r.get("paidAcceptance"))
        c["penalty"] += num(r.get("penalty"))
        c["deduction"] += num(r.get("deduction"))
        c["addPay"] += num(r.get("additionalPayment"))

    rows = []
    for (day, nm, model), v in sorted(facts.items()):
        if not day:
            continue
        item = {"d": day, "nm": nm, "m": model}
        for k, val in v.items():
            if val:
                item[k] = round(val, 2)
        rows.append(item)

    # ------------------------------------------------------------ FBS
    deadline_h = float(cfg.get("assembly_deadline_h", 48))
    warn_h = float(cfg.get("warn_hours", 12))
    wh_over = {str(k): float(v) for k, v in (cfg.get("warehouse_deadline_h") or {}).items()}
    fbs_rows = []
    for key, rec in fbs.items():
        created = parse(rec.get("createdAt"))
        sup = supplies.get(rec.get("supplyId") or "")
        stage, stage_key = stage_of(rec, sup)
        wh = whs.get(str(rec.get("warehouseId")), {})
        dl_h = wh_over.get(str(rec.get("warehouseId")), deadline_h)
        deadline = created + timedelta(hours=dl_h) if created else None
        pending = stage_key in ("queue", "assembly")
        left_h = (deadline - now).total_seconds() / 3600 if deadline else None
        st, st_key = supply_state(sup)
        fbs_rows.append({
            "id": rec.get("id"), "article": rec.get("article") or art_of.get(rec.get("nmId")),
            "nmId": rec.get("nmId"), "createdAt": rec.get("createdAt"),
            "deadlineAt": iso(deadline), "deadlineH": dl_h,
            "leftH": round(left_h, 2) if left_h is not None else None,
            "pending": pending,
            "overdue": bool(pending and left_h is not None and left_h < 0),
            "soon": bool(pending and left_h is not None and 0 <= left_h <= warn_h),
            "price": round(num(rec.get("price")) / 100, 2) if rec.get("price") else None,
            "warehouse": wh.get("name") or f"склад {rec.get('warehouseId')}",
            "office": ", ".join(rec.get("offices") or []),
            "supplyId": rec.get("supplyId"), "supplyState": st, "supplyStateKey": st_key,
            "supplierStatusRu": SUPPLIER_RU.get(rec.get("supplierStatus"),
                                                rec.get("supplierStatus") or "—"),
            "wbStatusRu": WB_RU.get(rec.get("wbStatus"), rec.get("wbStatus") or "—"),
            "stage": stage, "stageKey": stage_key,
        })
    fbs_rows.sort(key=lambda r: (not r["pending"],
                                 r["leftH"] if r["leftH"] is not None else 1e9))

    # ------------------------------------------- скорость сборки и отгрузки
    def hours(a, b):
        if not a or not b or b <= a:
            return None
        return (b - a).total_seconds() / 3600

    asm, ship = [], []
    for key, rec in fbs.items():
        created = parse(rec.get("createdAt"))
        sup = supplies.get(rec.get("supplyId") or "")
        left = parse(rec.get("leftQueueAt")) or parse((sup or {}).get("closedAt"))
        h = hours(created, left)
        if h is not None:
            asm.append(h)
        h = hours(created, parse((sup or {}).get("scanDt")))
        if h is not None:
            ship.append(h)

    def stat(xs):
        if not xs:
            return {"n": 0}
        xs = sorted(xs)
        return {"n": len(xs), "avg": round(sum(xs) / len(xs), 2),
                "med": round(xs[len(xs) // 2], 2),
                "p90": round(xs[min(len(xs) - 1, int(len(xs) * 0.9))], 2)}

    # шкала WB: от скорости отгрузки зависит поправка к комиссии
    BUCKETS = cfg.get("speed_buckets") or []

    def delta_of(h):
        lo = 0
        for b in BUCKETS:
            top = b.get("to")
            if top is None or h < top:
                if b.get("per_hour") is not None:
                    return b["per_hour"] * max(0.0, h - float(b.get("from_hour", 0)))
                return float(b.get("delta") or 0)
            lo = top
        return 0.0

    buckets = []
    lo = 0
    for b in BUCKETS:
        top = b.get("to")
        inside = [h for h in ship if h >= lo and (top is None or h < top)]
        buckets.append({
            "label": b.get("label"), "n": len(inside),
            "share": round(len(inside) / len(ship), 4) if ship else 0,
            "delta": round(sum(delta_of(h) for h in inside) / len(inside), 2) if inside
                     else (None if b.get("per_hour") is not None else float(b.get("delta") or 0)),
        })
        lo = top if top is not None else lo
    weighted = round(sum(delta_of(h) for h in ship) / len(ship), 2) if ship else None

    speed = {"assembly": stat(asm), "ship": stat(ship), "buckets": buckets,
             "delta": weighted, "basis": cfg.get("speed_basis", "scan")}

    sup_rows = []
    per_sup = defaultdict(list)
    for r in fbs_rows:
        if r["supplyId"]:
            per_sup[r["supplyId"]].append(r)
    for sid, items in per_sup.items():
        s = supplies.get(sid, {})
        st, st_key = supply_state(s)
        sup_rows.append({"id": sid, "name": s.get("name"), "createdAt": s.get("createdAt"),
                         "closedAt": s.get("closedAt"), "scanDt": s.get("scanDt"),
                         "state": st, "stateKey": st_key, "orders": len(items),
                         "articles": sorted({i["article"] for i in items if i["article"]})})
    sup_rows.sort(key=lambda x: x["createdAt"] or "", reverse=True)

    stock_rows, stock_by_nm = [], defaultdict(int)
    for wh, v in stocks.items():
        if not v.get("total"):
            continue
        w = whs.get(str(wh), {})
        by = defaultdict(int)
        for r in v.get("rows", []):
            by[r["nmId"]] += r["amount"]
            stock_by_nm[r["nmId"]] += r["amount"]
        stock_rows.append({"warehouse": w.get("name") or f"склад {wh}",
                           "cargo": CARGO.get(w.get("cargoType"), ""),
                           "total": v["total"], "at": v.get("at"),
                           "items": [{"article": art_of.get(k, str(k)), "amount": n}
                                     for k, n in sorted(by.items(), key=lambda x: -x[1])]})
    stock_rows.sort(key=lambda x: -x["total"])
    for a in articles:
        a["stock"] = stock_by_nm.get(a["nmId"], 0)

    # ------------------------------------------------ остатки FBO на складах WB
    PSEUDO = {"Всего находится на складах", "В пути до получателей",
              "В пути возвраты на склад WB"}
    fbo_by_wh, fbo_by_nm, fbo_transit = defaultdict(int), defaultdict(int), defaultdict(int)
    for v in fbo.values():
        nm = v.get("nmId")
        if nm not in art_of:
            continue
        for w in v.get("warehouses") or []:
            name, q = w.get("warehouseName"), w.get("quantity") or 0
            if name == "Всего находится на складах":
                continue
            if name in PSEUDO:
                fbo_transit[name] += q
                continue
            fbo_by_wh[name] += q
            fbo_by_nm[nm] += q
    fbo_rows = [{"warehouse": k, "total": v} for k, v in
                sorted(fbo_by_wh.items(), key=lambda x: -x[1])]
    for a in articles:
        a["fbo"] = fbo_by_nm.get(a["nmId"], 0)

    # ------------------------------------------------- когорты выкупа по дням
    mature = int(cfg.get("buyout_mature_days", 21))
    cohort = defaultdict(lambda: {"ord": 0, "bought": 0})
    for r in orders.values():
        nm = r.get("nmId")
        if nm not in art_of:
            continue
        d = day_of(r.get("date"))
        cohort[d]["ord"] += 1
        if r.get("srid") in sale_srids:
            cohort[d]["bought"] += 1
    edge = (now - timedelta(days=mature)).date().isoformat()
    mat_ord = sum(v["ord"] for d, v in cohort.items() if d <= edge)
    mat_buy = sum(v["bought"] for d, v in cohort.items() if d <= edge)

    data = {
        "meta": {
            "brand": cfg.get("brand"),
            "generatedAt": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "historyFrom": cfg.get("history_from"),
            "deadlineH": deadline_h, "warnH": warn_h,
            "commissionPct": float(cfg.get("commission_fixed_pct", 43)),
            "commissionMode": cfg.get("commission_mode", "fixed"),
            "matureDays": mature,
            "cabinetQueue": cursor.get("cabinet_queue"),
            "caughtUp": cursor.get("fbs_caught_up"),
            "lastRun": cursor.get("last_run"),
            "financeAt": cursor.get("finance_at"),
            "financeTo": cursor.get("finance_covered_to"),
            "financeReports": len(cursor.get("finance_reports") or {}),
            "cards": len(cards), "financeRows": len(finance),
            "ordersTracked": len(orders), "salesTracked": len(sales),
            "matureOrders": mat_ord, "matureBuyouts": mat_buy,
            "stockTotal": sum(r["total"] for r in stock_rows),
            "stockWarehouses": len(stock_rows),
            "fboTotal": sum(r["total"] for r in fbo_rows),
            "fboWarehouses": len(fbo_rows),
            "fboTransit": dict(fbo_transit),
            "fboAt": cursor.get("fbo_at"),
        },
        "articles": articles,
        "facts": rows,
        "cohort": [{"d": d, **v} for d, v in sorted(cohort.items())],
        "fbs": fbs_rows,
        "speed": speed,
        "supplies": sup_rows,
        "stocks": stock_rows,
        "fbo": fbo_rows,
    }
    os.makedirs(path("data"), exist_ok=True)
    with open(path("data", "dashboard_data.json"), "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, separators=(",", ":"))
    m = data["meta"]
    print(f"свод: артикулов {len(articles)}, строк факта {len(rows)}, "
          f"заказов {m['ordersTracked']}, продаж {m['salesTracked']}, "
          f"финотчёт {m['financeRows']} строк, FBS-заданий {len(fbs_rows)}", flush=True)


if __name__ == "__main__":
    main()
