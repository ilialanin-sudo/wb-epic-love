#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Вёрстка + данные → готовая страница dist/index.html."""
import json
import os

HERE = os.path.dirname(os.path.abspath(__file__))


def main():
    with open(os.path.join(HERE, "template.html"), encoding="utf-8") as f:
        tpl = f.read()
    with open(os.path.join(HERE, "data", "dashboard_data.json"), encoding="utf-8") as f:
        data = json.load(f)
    repo = os.environ.get("GITHUB_REPOSITORY") or ""
    data["meta"]["runUrl"] = (f"https://github.com/{repo}/actions/workflows/dashboard.yml"
                              if repo else "")
    data["meta"]["repo"] = repo
    payload = json.dumps(data, ensure_ascii=False, separators=(",", ":"))
    # закрывающий тег внутри строки оборвал бы <script>
    payload = payload.replace("</", "<\\/")
    html = tpl.replace("__DATA__", payload)
    html = html.replace("__BRAND__", str(data["meta"].get("brand") or ""))
    # себестоимость из секрета: попадает в страницу как значение по умолчанию
    cost = os.path.join(HERE, "cost.json")
    if os.path.exists(cost):
        try:
            with open(cost, encoding="utf-8") as f:
                by = (json.load(f) or {}).get("by_article") or {}
            html = html.replace("let COST = {};",
                                "let COST = " + json.dumps(by, ensure_ascii=False) + ";")
            print(f"себестоимость из секрета: {len(by)} артикулов", flush=True)
        except (ValueError, OSError) as e:
            print(f"cost.json не прочитан: {e}", flush=True)
    out = os.path.join(HERE, "dist")
    os.makedirs(out, exist_ok=True)
    with open(os.path.join(out, "index.html"), "w", encoding="utf-8") as f:
        f.write(html)
    # маячок для кнопки «обновить»: страница сама проверяет, не собралась ли новая версия
    with open(os.path.join(out, "version.json"), "w", encoding="utf-8") as f:
        json.dump({"generatedAt": data["meta"]["generatedAt"]}, f)
    print(f"страница: {os.path.join(out, 'index.html')} ({len(html)//1024} КБ)", flush=True)


if __name__ == "__main__":
    main()
