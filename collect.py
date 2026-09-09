#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Выгрузка данных одного бренда из мультибрендового кабинета Wildberries.

Источники:
  content-api        — карточки бренда: nmID, артикул, предмет, фото, штрихкоды;
  statistics/orders  — заказы, инкремент по lastChangeDate;
  statistics/sales   — продажи и возвраты, тот же инкремент;
  finance/detailed   — финотчёт: комиссия, эквайринг, логистика, хранение,
                       приёмка, штрафы, удержания, к перечислению;
  marketplace v3     — оперативка FBS: очередь сборки, статусы, поставки, остатки.

Кабинет отдаёт сотни тысяч строк по всем брендам, фильтра по бренду в API нет,
поэтому качаем поток целиком и оставляем только строки бренда. Состояние лежит
в state/ и коммитится в репозиторий — каждый прогон продолжает с курсора.
"""
import json
import os
import time
from datetime import datetime, timezone, timedelta

from wb_client import WBClient

HERE = os.path.dirname(os.path.abspath(__file__))
MP = "marketplace-api.wildberries.ru"
CONTENT = "content-api.wildberries.ru"
STAT = "statistics-api.wildberries.ru"
FIN = "finance-api.wildberries.ru"

FINAL_WB = {"sold", "canceled", "canceled_by_client", "declined_by_client", "defect"}


def path(*p):
    return os.path.join(HERE, *p)


def load(name, default):
    try:
        with open(path(name), encoding="utf-8") as f:
            return json.load(f)
    except (FileNotFoundError, ValueError):
        return default


def save(name, obj):
    os.makedirs(os.path.dirname(path(name)), exist_ok=True)
    with open(path(name), "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def now_iso():
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def parse(ts):
    if not ts:
        return None
    try:
        return datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
    except ValueError:
        return None


def num(v):
    try:
        return float(v)
    except (TypeError, ValueError):
        return 0.0


class Collector:
    def __init__(self):
        self.cfg = load("config.json", {})
        token = (os.environ.get("WB_TOKEN") or "").strip()
        if not token and os.path.exists(path(".env")):
            for line in open(path(".env"), encoding="utf-8"):
                if line.strip().startswith("WB_TOKEN="):
                    token = line.split("=", 1)[1].strip()
        if not token:
            raise SystemExit("нет токена: положите WB_TOKEN в окружение или в .env рядом")
        self.wb = WBClient(token, min_interval=1.6)
        self.prefixes = [p.strip().upper() for p in self.cfg.get("brand_prefixes", [])]

        self.cards = load("state/cards.json", {})
        self.orders = load("state/orders.json", {})          # заказы из статистики, по srid
        self.sales = load("state/sales.json", {})            # продажи и возвраты, по saleID
        self.finance = load("state/finance.json", {})        # финотчёт, по rrdId
        self.fbs = load("state/fbs_orders.json", {})         # сборочные задания
        self.supplies = load("state/supplies.json", {})
        self.stocks = load("state/stocks.json", {})
        self.warehouses = load("state/warehouses.json", {})
        self.fbo = load("state/fbo_remains.json", {})
        self.cursor = load("state/cursor.json", {})
        self.log = []

    # ------------------------------------------------------------- фильтры
    def is_brand(self, brand):
        b = (brand or "").strip().upper()
        return any(b.startswith(p) for p in self.prefixes)

    def nm_ids(self):
        return {int(k) for k in self.cards}

    def sku_map(self):
        """штрихкод → nmID: строки логистики в финотчёте приходят без nmId."""
        out = {}
        for nm, c in self.cards.items():
            for z in c.get("sizes") or []:
                for sku in z.get("skus") or []:
                    out[str(sku)] = int(nm)
        return out

    # -------------------------------------------------------- 1. карточки
    def collect_cards(self):
        last = parse(self.cursor.get("cards_at"))
        if last and datetime.now(timezone.utc) - last < timedelta(
                hours=float(self.cfg.get("cards_refresh_hours", 6))):
            return
        cursor = {"limit": 100}
        found = {}
        pages = 0
        while pages < 200:
            d = self.wb.post(CONTENT, "/content/v2/get/cards/list",
                             {"settings": {"cursor": cursor, "filter": {"withPhoto": -1}}}) or {}
            cs = d.get("cards", [])
            pages += 1
            for c in cs:
                if not self.is_brand(c.get("brand")):
                    continue
                ph = c.get("photos") or []
                found[str(c["nmID"])] = {
                    "vendorCode": c.get("vendorCode"), "brand": c.get("brand"),
                    "subject": c.get("subjectName"), "title": c.get("title"),
                    "photo": ph[0].get("tm") if ph else None,
                    "sizes": [{"techSize": z.get("techSize"), "skus": z.get("skus"),
                               "chrtID": z.get("chrtID")} for z in (c.get("sizes") or [])],
                }
            cur = d.get("cursor", {})
            if len(cs) < 100:
                break
            cursor = {"limit": 100, "updatedAt": cur.get("updatedAt"), "nmID": cur.get("nmID")}
        if found:
            self.cards = found
        self.cursor["cards_at"] = now_iso()
        self.log.append(f"карточек бренда: {len(self.cards)} (просмотрено {pages} стр.)")

    # ------------------------------------------- 2. заказы и продажи (статистика)
    def _pull_stat(self, path_, cursor_key, store, key_field, max_calls=12):
        nm = self.nm_ids()
        start = self.cursor.get(cursor_key) or (
            self.cfg.get("history_from", "2026-01-01") + "T00:00:00")
        got = kept = 0
        cur = start
        for _ in range(max_calls):
            rows = self.wb.get(STAT, path_, {"dateFrom": cur, "flag": 0}) or []
            if not rows:
                break
            got += len(rows)
            for r in rows:
                if r.get("nmId") in nm or self.is_brand(r.get("brand")):
                    store[str(r.get(key_field) or r.get("srid"))] = r
                    kept += 1
            last = max((r.get("lastChangeDate") or "") for r in rows)
            if len(rows) < 80000 or not last or last == cur:
                cur = last or cur
                break
            cur = last
        self.cursor[cursor_key] = cur
        self.log.append(f"{path_.rsplit('/', 1)[-1]}: просмотрено {got}, бренда {kept}, "
                        f"в базе {len(store)}")

    def collect_stats(self):
        last = parse(self.cursor.get("stats_at"))
        if last and datetime.now(timezone.utc) - last < timedelta(
                minutes=float(self.cfg.get("stats_refresh_min", 55))):
            return
        self._pull_stat("/api/v1/supplier/orders", "orders_from", self.orders, "srid")
        self._pull_stat("/api/v1/supplier/sales", "sales_from", self.sales, "saleID")
        self.cursor["stats_at"] = now_iso()

    # ------------------------------------------------------- 3. финотчёт
    def collect_finance(self, force=False):
        """Идём от списка отчётов реализации, а не вслепую по датам.

        WB выкладывает отчёты порциями — обычно за неделю, иногда за день, — и
        какое-то время правит уже выложенные. Поэтому каждый прогон забираем
        список, качаем те отчёты, которых ещё нет, и перекачиваем несколько
        последних: их цифры могут поменяться задним числом.
        """
        last = parse(self.cursor.get("finance_at"))
        hours = float(self.cfg.get("finance_refresh_hours", 6))
        if not force and last and datetime.now(timezone.utc) - last < timedelta(hours=hours):
            return
        nm = self.nm_ids()
        skus = self.sku_map()
        known = self.cursor.get("finance_reports") or {}
        start = self.cfg.get("history_from", "2026-01-01")[:10]
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        lst = self.wb.post(FIN, "/api/finance/v1/sales-reports/list",
                           {"dateFrom": start, "dateTo": today}) or []
        lst.sort(key=lambda r: (r.get("dateFrom") or "", r.get("reportId")))
        # в списке уже есть суммы отчёта — по ним видно, правил ли его WB.
        # качаем только новые и те, где цифры разошлись с прошлым разом
        def sign(r):
            return "|".join(str(r.get(k)) for k in
                            ("retailAmountSum", "forPaySum", "deliveryServiceSum",
                             "paidStorageSum", "paidAcceptanceSum", "deductionSum",
                             "penaltySum", "additionalPaymentSum", "createDate"))

        recheck = int(self.cfg.get("finance_recheck_reports", 1))
        forced = {str(r["reportId"]) for r in lst[-recheck:]} if recheck else set()
        todo = [r for r in lst
                if str(r["reportId"]) not in known
                or known[str(r["reportId"])].get("sign") != sign(r)
                or str(r["reportId"]) in forced]
        if not todo:
            self.cursor["finance_at"] = now_iso()
            self.log.append(f"финотчёт: новых отчётов нет, всего известно {len(known)}")
            return

        kept = seen = 0
        for rep in todo:
            rid = str(rep["reportId"])
            rows_all = []
            rrd = 0
            for _ in range(30):
                page = self.wb.post(FIN, f"/api/finance/v1/sales-reports/detailed/{rid}",
                                    {"rrdId": rrd, "limit": 100000})
                if not page:
                    break
                rows_all += page
                rrd = max(r["rrdId"] for r in page)
                if len(page) < 100000:
                    break
            seen += len(rows_all)
            # старые строки этого отчёта убираем: правки должны заменять, а не копиться
            for key in [k for k, v in self.finance.items() if str(v.get("reportId")) == rid]:
                del self.finance[key]
            for r in rows_all:
                n = r.get("nmId") or skus.get(str(r.get("sku") or ""))
                if n not in nm and not self.is_brand(r.get("brandName")):
                    continue
                r["_nmId"] = n or r.get("nmId") or 0
                self.finance[str(r["rrdId"])] = r
                kept += 1
            known[rid] = {"dateFrom": rep.get("dateFrom"), "dateTo": rep.get("dateTo"),
                          "createDate": rep.get("createDate"), "type": rep.get("reportType"),
                          "rows": len(rows_all), "at": now_iso(), "sign": sign(rep),
                          "forPaySum": rep.get("forPaySum")}
        self.cursor["finance_reports"] = known
        self.cursor["finance_at"] = now_iso()
        newest = max((v.get("dateTo") or "") for v in known.values()) if known else None
        self.cursor["finance_covered_to"] = newest
        self.log.append(f"финотчёт: обработано отчётов {len(todo)} из {len(lst)}, "
                        f"просмотрено {seen} строк, бренда {kept}, в базе {len(self.finance)}; "
                        f"закрыт по {newest}")

    # --------------------------------------------------------- 4. FBS
    def fbs_mine(self, o):
        return o.get("nmId") in self.nm_ids()

    def fbs_upsert(self, o, in_queue=False):
        key = str(o["id"])
        rec = self.fbs.get(key, {})
        keep = {k: o.get(k) for k in (
            "id", "article", "nmId", "chrtId", "skus", "createdAt", "warehouseId",
            "officeId", "offices", "price", "salePrice", "rid", "orderUid", "supplyId")}
        if keep.get("supplyId") is None and rec.get("supplyId"):
            keep["supplyId"] = rec["supplyId"]
        rec.update({k: v for k, v in keep.items() if v is not None})
        rec.setdefault("firstSeen", now_iso())
        rec["inQueue"] = True if in_queue else rec.get("inQueue", False)
        self.fbs[key] = rec

    def collect_fbs_queue(self):
        d = self.wb.get(MP, "/api/v3/orders/new") or {}
        allq = d.get("orders", [])
        self.cursor["cabinet_queue"] = len(allq)
        live = set()
        for o in allq:
            if self.fbs_mine(o):
                self.fbs_upsert(o, in_queue=True)
                live.add(str(o["id"]))
        for key, rec in self.fbs.items():
            if rec.get("inQueue") and key not in live:
                rec["inQueue"] = False
                rec.setdefault("leftQueueAt", now_iso())
        self.log.append(f"очередь сборки кабинета: {len(allq)}, из них бренда: {len(live)}")

    def collect_fbs_orders(self):
        cur = self.cursor.get("fbs_next")
        date_from = self.cursor.get("fbs_from")
        if cur is None or date_from is None:
            days = int(self.cfg.get("fbs_backfill_days", 3))
            date_from = int(time.time()) - days * 86400
            cur = 0
        limit = int(os.environ.get("WB_MAX_PAGES") or self.cfg.get("fbs_max_pages_per_run", 40))
        hits = pages = 0
        done = False
        for _ in range(limit):
            try:
                d = self.wb.get(MP, "/api/v3/orders",
                                {"limit": 1000, "next": cur, "dateFrom": date_from})
            except RuntimeError as e:
                self.log.append(f"инкремент FBS прерван: {e}")
                break
            page = (d or {}).get("orders", [])
            cur = (d or {}).get("next", cur)
            pages += 1
            for o in page:
                if self.fbs_mine(o):
                    self.fbs_upsert(o)
                    hits += 1
            if len(page) < 1000:
                done = True
                break
        self.cursor["fbs_next"] = cur
        self.cursor["fbs_from"] = date_from
        self.cursor["fbs_caught_up"] = done
        self.log.append(f"FBS инкремент: {pages} стр., бренда {hits}, "
                        + ("догнал" if done else "хвост остался"))

    def collect_fbs_status(self):
        ids = [int(k) for k, r in self.fbs.items()
               if not (r.get("statusSettled") and r.get("wbStatus") in FINAL_WB)]
        if not ids:
            return
        got = 0
        for i in range(0, len(ids), 1000):
            d = self.wb.post(MP, "/api/v3/orders/status", {"orders": ids[i:i + 1000]}) or {}
            for s in d.get("orders", []):
                rec = self.fbs.get(str(s["id"]))
                if not rec:
                    continue
                if rec.get("wbStatus") != s.get("wbStatus"):
                    rec["statusChangedAt"] = now_iso()
                rec["supplierStatus"] = s.get("supplierStatus")
                rec["wbStatus"] = s.get("wbStatus")
                rec["statusAt"] = now_iso()
                if s.get("wbStatus") in FINAL_WB:
                    rec["statusSettled"] = True
                got += 1
        self.log.append(f"статусы FBS обновлены: {got}")

    def collect_supplies(self):
        need = {r["supplyId"] for r in self.fbs.values() if r.get("supplyId")}
        need = {s for s in need
                if not self.supplies.get(s) or not self.supplies[s].get("scanDt")}
        for sid in sorted(need):
            try:
                d = self.wb.get(MP, f"/api/v3/supplies/{sid}")
            except RuntimeError as e:
                self.log.append(f"поставка {sid}: {e}")
                continue
            if d:
                self.supplies[sid] = d
        if need:
            self.log.append(f"поставок обновлено: {len(need)}")

    def collect_warehouses(self):
        last = parse(self.cursor.get("wh_at"))
        if last and datetime.now(timezone.utc) - last < timedelta(hours=24):
            return
        for w in (self.wb.get(MP, "/api/v3/warehouses") or []):
            self.warehouses[str(w["id"])] = {"name": w.get("name"), "officeId": w.get("officeId"),
                                             "cargoType": w.get("cargoType")}
        self.cursor["wh_at"] = now_iso()
        self.log.append(f"складов продавца: {len(self.warehouses)}")

    def collect_stocks(self):
        skus = self.sku_map()
        if not skus:
            return
        last = parse(self.cursor.get("stocks_full_at"))
        hours = float(self.cfg.get("stocks_refresh_hours", 4))
        full = not last or datetime.now(timezone.utc) - last > timedelta(hours=hours)
        targets = list(self.warehouses) if full else [w for w, v in self.stocks.items()
                                                     if v.get("total")]
        if not targets:
            return
        keys = list(skus.keys())
        fresh = {}
        for wh in targets:
            rows = []
            try:
                for i in range(0, len(keys), 1000):
                    d = self.wb.post(MP, f"/api/v3/stocks/{wh}", {"skus": keys[i:i + 1000]}) or {}
                    rows += [r for r in d.get("stocks", []) if (r.get("amount") or 0) > 0]
            except RuntimeError as e:
                self.log.append(f"остатки, склад {wh}: {e}")
                continue
            total = sum(r["amount"] for r in rows)
            if not total:
                continue
            fresh[wh] = {"total": total, "at": now_iso(), "rows": [
                {"sku": r["sku"], "amount": r["amount"], "nmId": skus[r["sku"]]}
                for r in rows if r["sku"] in skus]}
        if full:
            self.stocks = fresh
            self.cursor["stocks_full_at"] = now_iso()
        else:
            self.stocks.update(fresh)
        self.log.append(f"остаток FBS: {sum(v['total'] for v in self.stocks.values())} шт "
                        f"на {len(self.stocks)} складах")

    # ------------------------------------------- остатки FBO на складах WB
    def collect_fbo(self):
        last = parse(self.cursor.get("fbo_at"))
        hours = float(self.cfg.get("stocks_refresh_hours", 4))
        if last and datetime.now(timezone.utc) - last < timedelta(hours=hours):
            return
        A = "seller-analytics-api.wildberries.ru"
        d = self.wb.get(A, "/api/v1/warehouse_remains",
                        {"groupByNm": "true", "groupBySize": "true"}) or {}
        task = (d.get("data") or {}).get("taskId")
        if not task:
            self.log.append("остатки FBO: задача не создалась")
            return
        for _ in range(24):
            time.sleep(8)
            st = self.wb.get(A, f"/api/v1/warehouse_remains/tasks/{task}/status") or {}
            if ((st.get("data") or {}).get("status")) == "done":
                break
        else:
            self.log.append("остатки FBO: отчёт не успел собраться, возьму в следующий прогон")
            return
        rows = self.wb.get(A, f"/api/v1/warehouse_remains/tasks/{task}/download") or []
        nm = self.nm_ids()
        keep = {}
        for r in rows:
            if r.get("nmId") not in nm:
                continue
            key = f"{r['nmId']}|{r.get('techSize') or ''}"
            keep[key] = {"nmId": r["nmId"], "techSize": r.get("techSize"),
                         "warehouses": r.get("warehouses") or []}
        self.fbo = keep
        self.cursor["fbo_at"] = now_iso()
        total = sum(w["quantity"] for v in keep.values() for w in v["warehouses"]
                    if w["warehouseName"] not in ("Всего находится на складах",
                                                  "В пути до получателей",
                                                  "В пути возвраты на склад WB"))
        self.log.append(f"остатки FBO: {total} шт по {len(keep)} размерам "
                        f"(просмотрено {len(rows)} строк отчёта)")

    # ------------------------------------------------------------ прогон
    def run(self):
        t0 = time.time()
        force_fin = bool(os.environ.get("WB_FORCE_FINANCE"))
        steps = [
            ("карточки", self.collect_cards),
            ("склады", self.collect_warehouses),
            ("статистика", self.collect_stats),
            ("финотчёт", lambda: self.collect_finance(force_fin)),
            ("очередь FBS", self.collect_fbs_queue),
            ("заказы FBS", self.collect_fbs_orders),
            ("статусы FBS", self.collect_fbs_status),
            ("поставки", self.collect_supplies),
            ("остатки FBS", self.collect_stocks),
            ("остатки FBO", self.collect_fbo),
        ]
        failed = []
        for name, fn in steps:
            try:
                fn()
            except Exception as e:                       # noqa: BLE001
                failed.append(name)
                self.log.append(f"шаг «{name}» не отработал: {e}")
        self.cursor["last_run"] = now_iso()
        self.cursor["failed"] = failed
        for name, obj in (("cards", self.cards), ("orders", self.orders), ("sales", self.sales),
                          ("finance", self.finance), ("fbs_orders", self.fbs),
                          ("supplies", self.supplies), ("stocks", self.stocks),
                          ("warehouses", self.warehouses), ("fbo_remains", self.fbo),
                          ("cursor", self.cursor)):
            save(f"state/{name}.json", obj)
        for line in self.log:
            print(line, flush=True)
        print(f"готово за {time.time() - t0:.0f} с"
              + (f"; сбойные шаги: {', '.join(failed)}" if failed else ""), flush=True)


if __name__ == "__main__":
    Collector().run()
