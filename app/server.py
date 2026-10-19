"""Loopback-only application server; no dependencies outside Python's standard library."""
from __future__ import annotations

import argparse
import hmac
import json
import mimetypes
import os
import secrets
import sqlite3
import sys
import threading
import urllib.parse
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

if __package__ in (None, ""):
    sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app import __version__ as VERSION
from app.ai import AIError, KeyStore, ModelClient, Worker, validate_config
from app.store import Store, compact, day_valid, tzinfo
from app.usage import UsageLedger, call_scope

ROOT = Path(__file__).resolve().parent.parent
WEB = ROOT / "web"


class Application:
    def __init__(self, data_dir):
        self.store = Store(data_dir)
        self.keys = KeyStore(self.store.directory)
        self.usage = UsageLedger(self.store.directory)
        self.client = ModelClient(self.keys, self.usage)
        self.worker = Worker(self.store, self.client)
        self.token = secrets.token_urlsafe(40)

    def public_settings(self):
        values = self.store.settings()
        return {**values, "key_present": self.keys.path.exists(), "data_dir": str(self.store.directory),
                "database_path": str(self.store.path), "effective_backup_dir": str(Path(values["backup_dir"] or self.store.directory / "backups").resolve())}

    def settings_input(self, data):
        values = self.store.settings()
        for k in ("timezone", "ai_mode", "ai_base_url", "ai_model", "cloud_consent", "backup_keep", "backup_dir"):
            if k in data:
                values[k] = data[k]
        tzinfo(values["timezone"])
        if not isinstance(values["backup_keep"], int) or not 2 <= values["backup_keep"] <= 100:
            raise ValueError("备份保留数量需为 2–100")
        if not isinstance(values["backup_dir"], str) or len(values["backup_dir"]) > 1000:
            raise ValueError("备份目录无效")
        if values["backup_dir"] and not Path(values["backup_dir"]).is_absolute():
            raise ValueError("备份目录需要完整的绝对路径")
        if not isinstance(values["cloud_consent"], bool):
            raise ValueError("请确认云端传输选择")
        if not isinstance(values["ai_base_url"], str) or len(values["ai_base_url"]) > 2000:
            raise ValueError("服务地址无效")
        values["ai_base_url"] = values["ai_base_url"].strip()
        if not isinstance(values["ai_model"], str):
            raise ValueError("模型名称格式无效")
        values["ai_model"] = values["ai_model"].strip()
        if "api_key" in data and (not isinstance(data["api_key"], str) or len(data["api_key"]) > 4000 or any(ord(c) < 32 for c in data["api_key"])):
            raise ValueError("密钥格式无效")
        validate_config(values)
        return values


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "ShiguangLocal/1"

    @property
    def app(self):
        return self.server.application

    def log_message(self, *_):
        pass

    def send(self, data, status=200, content_type="application/json; charset=utf-8", filename=None):
        raw = data if isinstance(data, bytes) else compact(data).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("Cross-Origin-Resource-Policy", "same-origin")
        self.send_header("Content-Security-Policy", "default-src 'self'; script-src 'self'; style-src 'self'; img-src 'self' data:; connect-src 'self'; font-src 'self'; base-uri 'none'; frame-ancestors 'none'; form-action 'self'; object-src 'none'")
        if filename:
            self.send_header("Content-Disposition", "attachment; filename=\"" + filename + "\"")
        self.end_headers()
        try:
            self.wfile.write(raw)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def guard(self, mutation=False, token=True):
        port = self.server.server_port
        allowed_hosts = {"127.0.0.1:" + str(port), "localhost:" + str(port)}
        if self.headers.get("Host") not in allowed_hosts:
            raise PermissionError("仅允许通过本机地址访问")
        origin = self.headers.get("Origin")
        if origin and origin not in {"http://" + h for h in allowed_hosts}:
            raise PermissionError("请求来源不受信任")
        if self.headers.get("Sec-Fetch-Site") == "cross-site":
            raise PermissionError("不允许跨站请求")
        if token and not hmac.compare_digest(self.headers.get("X-Notes-Token", ""), self.app.token):
            raise PermissionError("连接已更新，请刷新页面后重试")
        if mutation and self.headers.get_content_type() != "application/json":
            raise ValueError("需要 JSON 格式请求")

    def body(self):
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 30_000_000:
                raise ValueError("请求内容为空或超过 30 MB")
            data = json.loads(self.rfile.read(length))
            if not isinstance(data, dict):
                raise ValueError("请求内容应为对象")
            return data
        except (json.JSONDecodeError, UnicodeError):
            raise ValueError("请求格式不正确") from None

    def dispatch(self, mutation=False):
        try:
            path = urllib.parse.urlsplit(self.path).path
            query = {k: v[0] for k, v in urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).items()}
            self.guard(mutation, token=path.startswith("/api/") and path not in ("/api/bootstrap", "/api/health"))
            if mutation:
                if path in ("/api/bootstrap", "/api/health"):
                    raise PermissionError("此接口仅支持读取")
                self.mutate(path, self.body())
            else:
                self.read(path, query)
        except PermissionError as exc:
            self.send({"error": str(exc)}, 403)
        except (ValueError, AIError) as exc:
            self.send({"error": str(exc)}, 400)
        except sqlite3.Error:
            self.send({"error": "数据库操作失败，原有数据未被替换；请检查磁盘空间或重试"}, 500)
        except Exception:
            self.send({"error": "本地操作未完成，请稍后重试；输入内容应保留在页面中"}, 500)

    def do_GET(self):
        self.dispatch()

    def do_POST(self):
        self.dispatch(mutation=True)

    def read(self, path, query):
        store = self.app.store
        if path == "/api/health":
            return self.send({"app": "shiguang-diary", "version": VERSION})
        if path == "/api/bootstrap":
            return self.send({"token": self.app.token, "today": store.today(), "settings": self.app.public_settings(), "version": VERSION})
        if path == "/api/state":
            day = day_valid(query.get("day", store.today()))
            search = query.get("q", "")[:300]
            return self.send({"today": store.today(), "entries": store.entries(None if search else day, search),
                              "calendar": store.calendar(), "actions": store.actions(), "summary": store.summary(day), "settings": self.app.public_settings()})
        if path == "/api/history":
            with store.connection() as db:
                rows = [dict(r) for r in db.execute("SELECT * FROM entry_versions WHERE entry_id=? ORDER BY revision DESC", (query.get("id"),))]
            return self.send({"versions": rows})
        if path == "/api/about":
            result = store.about_state(query.get("month", store.today()[:7]))
            result["growth"]["usage"] = self.app.usage.growth_usage(store.settings())
            return self.send(result)
        if path == "/api/growth/timeline":
            return self.send(store.timeline(query.get("q", ""), query.get("kind", "all"), int(query.get("offset", "0"))))
        if path == "/api/usage":
            result = self.app.usage.state(store.settings(), query.get("month", store.today()[:7]), int(query.get("offset", "0")))
            with store.connection() as db:
                for row in result["records"]:
                    if row["kind"] == "entry":
                        source = db.execute("SELECT journal_day FROM entries WHERE id=?", (row["target"],)).fetchone()
                        row["source_day"] = source[0] if source else None
            return self.send(result)
        if path == "/api/usage/export":
            return self.send(self.app.usage.export_csv(query.get("month", "all"), store.settings()["timezone"]),
                             content_type="text/csv; charset=utf-8", filename="shiguang-usage.csv")
        if path == "/api/backups":
            return self.send({"backups": store.backups()})
        if path == "/api/export":
            data = store.export()
            if query.get("format") == "markdown":
                lines = ["# 拾光 · 日记导出", "", "导出时间：" + data["exported_at"], ""]
                records = sorted(data["tables"]["entries"], key=lambda e: (e["journal_day"], e["created_at"]))
                analyses = {a["entry_id"]: json.loads(a["data"]) for a in data["tables"]["analyses"]}
                for e in records:
                    lines.extend(["## " + e["journal_day"], "", "记录时间：" + e["created_at"] + " · " + e["timezone"], "", e["body"], ""])
                    if e["id"] in analyses:
                        lines.extend(["### AI 整理", "", analyses[e["id"]]["summary"], ""])
                lines.extend(["# 行动", ""])
                for a in data["tables"]["actions"]:
                    lines.append("- [%s] %s（%s / %s）" % ("x" if a["status"] == "done" else " ", a["title"], a["kind"], a["status"]))
                lines.extend(["", "# 成长经历", ""])
                for experience in data["tables"]["experiences"]:
                    lines.extend(["## " + experience["title"], "", experience["period"] + " · " + experience["chapter"], "", experience["body"], ""])
                lines.extend(["# 长期记忆", ""])
                for memory in store.memories():
                    lines.extend(["## " + memory["title"], "", (memory["correction"] if memory["status"] == "corrected" else memory["detail"]),
                                  "", "状态：" + {"pending": "待确认", "confirmed": "已确认", "corrected": "已修正"}[memory["status"]], ""])
                return self.send("\n".join(lines).encode(), content_type="text/markdown; charset=utf-8", filename="shiguang-diary.md")
            return self.send(json.dumps(data, ensure_ascii=False, indent=2).encode(), filename="shiguang-backup.json")
        files = {"/": "index.html", "/index.html": "index.html", "/app.js": "app.js", "/about.js": "about.js", "/usage.js": "usage.js", "/styles.css": "styles.css", "/favicon.svg": "favicon.svg"}
        if path in files:
            file = WEB / files[path]
            content = mimetypes.guess_type(file.name)[0] or "application/octet-stream"
            return self.send(file.read_bytes(), content_type=content + ("; charset=utf-8" if content.startswith("text/") or content == "application/javascript" else ""))
        return self.send({"error": "页面不存在"}, 404)

    def mutate(self, path, data):
        store = self.app.store
        result = {"ok": True}
        mutated = False
        if path == "/api/entries/save":
            result = {"entry": store.save_entry(data, data.get("entry_id"))}
            mutated = True
        elif path == "/api/entries/delete":
            store.delete_entry(data.get("id"))
            mutated = True
        elif path == "/api/actions/update":
            result = {"history_id": store.update_action(data.get("id"), data)}
            mutated = True
        elif path == "/api/experiences/save":
            result = {"experience": store.save_experience(data)}
            mutated = True
        elif path == "/api/experiences/delete":
            store.delete_experience(data.get("id"))
            mutated = True
        elif path == "/api/memory/permission":
            store.set_memory_permission(data.get("id"), data.get("enabled"))
            mutated = True
        elif path == "/api/memory/update":
            store.update_memory(data.get("id"), data)
            mutated = True
        elif path == "/api/memory/forget":
            store.forget_memory(data.get("id"))
            mutated = True
        elif path == "/api/about/generate":
            if store.settings()["ai_mode"] == "off":
                raise ValueError("请先在偏好与设置中启用 AI")
            store.request_about(data.get("kind"), data.get("target", "current"))
        elif path == "/api/growth/policy":
            result = {"policy": store.set_growth_policy(data)}
            mutated = True
        elif path == "/api/actions/undo":
            store.undo_action(data.get("history_id"))
            mutated = True
        elif path == "/api/retry":
            store.retry(data.get("entry_id"), data.get("day"))
        elif path == "/api/settings":
            values = self.app.settings_input(data)
            previous = store.settings()
            changed_service = values["ai_base_url"] != previous["ai_base_url"]
            if "api_key" in data:
                self.app.keys.set(data["api_key"])
            elif changed_service:
                self.app.keys.set("")
            if data.get("clear_key"):
                self.app.keys.set("")
            store.save_settings(values)
            result = {"settings": self.app.public_settings()}
        elif path == "/api/ai/test":
            values = self.app.settings_input(data)
            if values["ai_mode"] == "off":
                raise ValueError("请先选择本地或云端模式")
            existing_keys = self.app.keys
            previous = store.settings()
            same_service = values["ai_base_url"] == previous["ai_base_url"]

            class TestKeys:
                def get(self):
                    return data.get("api_key") or (existing_keys.get() if same_service and not data.get("clear_key") else "")

            with call_scope("test"):
                response = ModelClient(TestKeys(), self.app.usage).call(values, '只输出 JSON：{"ok":true}', {"message": "连接测试；不包含日记内容"})
            if response.get("ok") is not True:
                raise AIError("服务可以连接，但模型未按要求返回 JSON")
        elif path == "/api/backup":
            result = {"name": store.backup(force=True, label="manual")}
            self.app.usage.backup(store.settings())
        elif path == "/api/usage/price":
            self.app.usage.save_price(store.settings(), data)
            self.app.usage.backup(store.settings())
        elif path == "/api/restore":
            result = store.restore_backup(data.get("name"))
        elif path == "/api/import/preview":
            result = store.validate_import(data.get("bundle"))
        elif path == "/api/import":
            result = store.import_data(data.get("bundle"), replace=data.get("mode") == "replace")
        elif path == "/api/shutdown":
            self.app.worker.stop.set()
            threading.Thread(target=self.server.shutdown, daemon=True).start()
        else:
            return self.send({"error": "操作不存在"}, 404)
        if mutated:
            try:
                store.backup()
            except Exception:
                result["warning"] = "记录已保存，但自动备份失败，请到设置检查"
        self.send(result)


class LocalServer(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def get_request(self):
        request, address = super().get_request()
        request.settimeout(90)
        return request, address


def make_server(directory, port=0):
    server = LocalServer(("127.0.0.1", port), Handler)
    server.application = Application(directory)
    return server


def main():
    parser = argparse.ArgumentParser(description="拾光 · 本地日记")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument("--data-dir", default=os.environ.get("SHIGUANG_DATA_DIR") or str(ROOT / "data"))
    args = parser.parse_args()
    server = make_server(args.data_dir, args.port)
    app = server.application
    app.worker.start()
    (app.store.directory / "runtime.json").write_text(compact({"pid": os.getpid(), "port": server.server_port, "app": "shiguang-diary"}), encoding="utf-8")
    if sys.stdout:
        print("Local: http://127.0.0.1:%s" % server.server_port, flush=True)
    try:
        server.serve_forever(poll_interval=0.2)
    except KeyboardInterrupt:
        pass
    finally:
        app.worker.stop.set()
        server.server_close()


if __name__ == "__main__":
    main()
