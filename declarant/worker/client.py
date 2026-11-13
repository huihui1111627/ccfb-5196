"""Playwright driver for the mock customs declaration system.

Design notes
------------
* Every UI action is wrapped so that a session redirect to /login is detected,
  the worker re-authenticates and replays the *current* step (not the last one
  it recorded). Item progress is tracked per goods line in SQLite, so a crash
  or expiry resumes at the first unfinished line.
* Submission is guarded on both sides: the mock enforces a unique client_ref,
  and this driver re-reads the declaration status from the UI before ever
  clicking submit again.
"""
from __future__ import annotations

import json
import re
import time
from datetime import datetime
from pathlib import Path
from typing import Any, Callable

from playwright.sync_api import (Browser, BrowserContext, Error as PWError, Page,
                                 TimeoutError as PWTimeout, sync_playwright)

from declarant.common import catalog, data_dir, hs_entry, normalize_hs_code

STATUS_TEXT = {"草稿": "draft", "已申报": "submitted", "审核通过": "approved",
               "已退单": "rejected"}


class SessionExpired(Exception):
    """Raised when the mock system bounces us back to the login page."""


class StepFailed(Exception):
    def __init__(self, step: str, message: str, screenshot: str | None = None):
        super().__init__(message)
        self.step = step
        self.message = message
        self.screenshot = screenshot


def _safe_name(text: str) -> str:
    return re.sub(r"[^0-9A-Za-z_.-]+", "_", text)[:80]


class MockCustomsClient:
    def __init__(self, base_url: str, username: str, password: str,
                 headless: bool = True, profile_dir: Path | None = None,
                 log: Callable[..., None] | None = None,
                 screenshot_dir: Path | None = None):
        self.base_url = base_url.rstrip("/")
        self.username = username
        self.password = password
        self.headless = headless
        self.profile_dir = profile_dir
        self.log = log or (lambda *a, **k: None)
        self.screenshot_dir = screenshot_dir or (data_dir() / "screenshots")
        self._pw = None
        self.browser: Browser | None = None
        self.context: BrowserContext | None = None
        self.page: Page | None = None
        self.shot_index = 0
        self.renewals = 0
        self.relogins = 0

    # ------------------------------------------------------------------
    # lifecycle
    # ------------------------------------------------------------------
    def __enter__(self) -> "MockCustomsClient":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.stop()

    def start(self) -> None:
        self._pw = sync_playwright().start()
        launch_args = {"headless": self.headless}
        try:
            self.browser = self._pw.chromium.launch(**launch_args)
        except PWError:
            self.browser = self._pw.chromium.launch(channel="chrome", **launch_args)
        ctx_args: dict[str, Any] = {"viewport": {"width": 1440, "height": 1000},
                                    "locale": "zh-CN"}
        self.context = self.browser.new_context(**ctx_args)
        self.context.set_default_timeout(15_000)
        self.page = self.context.new_page()
        self.page.on("dialog", lambda d: d.accept())
        self.log("info", "browser_start", "浏览器已启动")

    def stop(self) -> None:
        for obj in (self.context, self.browser):
            try:
                if obj:
                    obj.close()
            except Exception:
                pass
        try:
            if self._pw:
                self._pw.stop()
        except Exception:
            pass
        self.page = None
        self.context = None
        self.browser = None
        self._pw = None

    # ------------------------------------------------------------------
    # screenshot + session helpers
    # ------------------------------------------------------------------
    def screenshot(self, tag: str) -> str:
        if not self.page:
            return ""
        self.shot_index += 1
        self.screenshot_dir.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = self.screenshot_dir / f"{stamp}_{self.shot_index:03d}_{_safe_name(tag)}.png"
        try:
            self.page.screenshot(path=str(path), full_page=True)
            return path.relative_to(data_dir()).as_posix()
        except Exception:
            return ""

    def _on_login_page(self) -> bool:
        if not self.page:
            return True
        return "/login" in self.page.url or self.page.locator("#login-btn").count() > 0

    def ensure_login(self) -> None:
        assert self.page is not None
        if not self._on_login_page():
            # cheap probe: the nav only exists when authenticated
            if self.page.locator(".mainnav").count() > 0:
                return
        self.login()

    def login(self) -> None:
        assert self.page is not None
        self.relogins += 1
        self.log("info", "login", f"登录 {self.base_url}")
        self.page.goto(f"{self.base_url}/login", wait_until="domcontentloaded")
        self.page.fill("#username", self.username)
        self.page.fill("#password", self.password)
        self.shot_index += 1
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            self.page.click("#login-btn")
        if self._on_login_page():
            raise StepFailed("login", "登录失败，请检查账号密码")
        self.log("info", "login_ok", "登录成功")
        self.screenshot("00_login_ok")

    def current_session_expiry(self) -> str | None:
        """Read the visible session countdown (MM:SS) and convert to a timestamp."""
        if not self.page or self._on_login_page():
            return None
        try:
            txt = self.page.locator("#session-chip").inner_text(timeout=2000)
        except Exception:
            return None
        m = re.search(r"(\d+):(\d+)", txt or "")
        if not m:
            return None
        seconds = int(m.group(1)) * 60 + int(m.group(2))
        return (datetime.now().replace(microsecond=0) +
                __import__("datetime").timedelta(seconds=seconds)).strftime("%Y-%m-%d %H:%M:%S")

    # ------------------------------------------------------------------
    # form helpers
    # ------------------------------------------------------------------
    def _fill(self, selector: str, value: Any) -> None:
        assert self.page is not None
        el = self.page.locator(selector)
        el.wait_for(state="visible", timeout=15_000)
        tag = el.evaluate("e => e.tagName.toLowerCase()")
        if tag == "select":
            try:
                el.select_option(str(value))
            except Exception:
                options = el.evaluate("e => Array.from(e.options).map(o => o.value)")
                match = next((o for o in options if o and str(value).startswith(o)), None)
                el.select_option(match or str(value))
        else:
            el.fill("")
            el.fill(str(value))
            # real systems load dependent fields on blur/change; dispatch it explicitly
            el.evaluate("e => { e.dispatchEvent(new Event('input', {bubbles:true}));"
                        " e.dispatchEvent(new Event('change', {bubbles:true}));"
                        " e.dispatchEvent(new Event('blur', {bubbles:true})); }")

    def _select_with_label_fallback(self, selector: str, value: str) -> None:
        """Real declaration systems accept either code or label; mock accepts codes."""
        try:
            self._fill(selector, value)
        except Exception:
            assert self.page is not None
            options = self.page.locator(selector).evaluate(
                "e => Array.from(e.options).map(o => ({v:o.value,t:o.textContent}))")
            match = next((o for o in options if value and value in o["t"]), None)
            if not match:
                raise
            self._fill(selector, match["v"])

    # ------------------------------------------------------------------
    # invoice -> declaration
    # ------------------------------------------------------------------
    def lookup(self, client_ref: str) -> list[dict[str, str]]:
        """Query the system by client reference / invoice number via the UI."""
        assert self.page is not None
        self.ensure_login()
        self.page.goto(f"{self.base_url}/lookup?ref={client_ref}",
                       wait_until="domcontentloaded")
        if self._on_login_page():
            self.login()
            self.page.goto(f"{self.base_url}/lookup?ref={client_ref}",
                           wait_until="domcontentloaded")
        rows = []
        for tr in self.page.locator("table.grid tbody tr").all():
            cells = tr.locator("td").all_inner_texts()
            if len(cells) < 4:
                continue
            links = tr.locator("a").all()
            href = links[0].get_attribute("href") if links else ""
            m = re.search(r"/declarations/(\d+)", href or "")
            if cells[0].strip() in ("暂无", "") or "未找到" in cells[0]:
                continue
            status_text = cells[3].strip()
            rows.append({"mock_decl_id": m.group(1) if m else "",
                         "decl_no": cells[0].strip(), "client_ref": cells[1].strip(),
                         "invoice_no": cells[2].strip(), "status_text": status_text,
                         "status": STATUS_TEXT.get(status_text, status_text),
                         "item_count": cells[4].strip() if len(cells) > 4 else "",
                         "submitted_at": cells[5].strip() if len(cells) > 5 else ""})
        return rows

    def open_or_create(self, decl: dict[str, Any]) -> dict[str, Any]:
        """Idempotent declaration creation.

        Returns {"mock_decl_id", "created", "status"} where status is the current
        status in the customs system. Never creates a second declaration for the
        same client_ref.
        """
        assert self.page is not None
        ref = decl["client_ref"]
        existing = self.lookup(ref)
        if existing:
            row = existing[0]
            mock_id = self._decl_id_from_lookup(row)
            self.log("info", "declaration_exists",
                     f"系统中已存在该客户参考号（{row['decl_no'] or '无单号'} / {row['status_text']}），复用不重复建单")
            self.screenshot(f"lookup_exists_{ref}")
            return {"mock_decl_id": mock_id, "created": False, "status": row["status"],
                    "decl_no": row["decl_no"]}

        self.page.goto(f"{self.base_url}/declarations/new", wait_until="domcontentloaded")
        if self._on_login_page():
            self.login()
            self.page.goto(f"{self.base_url}/declarations/new", wait_until="domcontentloaded")
        self._fill("#client_ref", ref)
        self._fill("#invoice_no", decl.get("invoice_no") or ref)
        self._select_with_label_fallback("#trade_mode", decl["trade_mode"])
        self._fill("#item_count", len(decl["items"]))
        self.screenshot(f"new_filled_{ref}")
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            self.page.click("#create-btn")
        content = self.page.content()
        if "重复申报" in content:
            rows = self.lookup(ref)
            if not rows:
                raise StepFailed("create", "系统报告重复申报，但查询不到记录，需人工核实")
            row = rows[0]
            return {"mock_decl_id": self._decl_id_from_lookup(row), "created": False,
                    "status": row["status"], "decl_no": row["decl_no"]}
        mock_id = self._decl_id_from_url(self.page.url)
        if not mock_id:
            raise StepFailed("create", "创建报关单后未跳转到表头页，无法继续")
        self.log("info", "declaration_created", f"预录入建单成功 ID={mock_id}")
        self.screenshot(f"created_{ref}")
        return {"mock_decl_id": mock_id, "created": True, "status": "draft", "decl_no": ""}

    @staticmethod
    def _decl_id_from_url(url: str) -> str | None:
        m = re.search(r"/declarations/(\d+)", url)
        return m.group(1) if m else None

    def _decl_id_from_lookup(self, row: dict[str, str]) -> str:
        return row.get("mock_decl_id") or row["client_ref"]

    # ------------------------------------------------------------------
    # header
    # ------------------------------------------------------------------
    HEADER_FIELDS = {
        "invoice_no": "#invoice_no",
        "contract_no": "#contract_no",
        "port_code": "#port_code",
        "transport_mode": "#transport_mode",
        "vessel_voyage": "#vessel_voyage",
        "trade_country": "#trade_country",
        "dest_country": "#dest_country",
        "currency": "#currency",
        "exchange_rate": "#exchange_rate",
        "package_count": "#package_count",
        "packing_type": "#packing_type",
        "gross_weight": "#gross_weight",
        "net_weight": "#net_weight",
        "consignee": "#consignee",
        "consignor": "#consignor",
    }

    def fill_header(self, decl: dict[str, Any], wait_linked: int = 20_000) -> None:
        assert self.page is not None
        mock_id = str(decl["mock_decl_id"])
        self.goto_declaration(mock_id)
        self.page.goto(f"{self.base_url}/declarations/{mock_id}/header",
                       wait_until="domcontentloaded")
        self._guard_login(decl, lambda: self.fill_header(decl, wait_linked))
        if "不能修改表头" in self.page.content():
            self.log("warn", "header_locked", "表头已申报，跳过表头录入")
            return
        self._select_with_label_fallback("#trade_mode", decl["trade_mode"])
        linked_fields = self._wait_linked_fields(decl["trade_mode"], wait_linked)
        self.log("info", "linked_fields_ready",
                 "联动字段已出现：" + ", ".join(f["name"] for f in linked_fields))
        self.screenshot(f"header_linked_{decl['client_ref']}")

        values = {k: decl.get(k) for k in self.HEADER_FIELDS}
        for key in ("invoice_no", "contract_no", "port_code", "transport_mode",
                    "vessel_voyage", "trade_country", "dest_country", "currency"):
            if values.get(key):
                try:
                    self._select_with_label_fallback(self.HEADER_FIELDS[key], values[key])
                except Exception:
                    self._fill(self.HEADER_FIELDS[key], values[key])
        for key in ("exchange_rate", "package_count", "gross_weight", "net_weight"):
            if values.get(key) not in (None, ""):
                self._fill(self.HEADER_FIELDS[key], values[key])
        if decl.get("packing_type"):
            self._select_with_label_fallback("#packing_type", decl["packing_type"])
        for key in ("consignee", "consignor"):
            if values.get(key):
                self._fill(self.HEADER_FIELDS[key], values[key])
        # linked fields: wait for each and fill
        for f in linked_fields:
            name = f["name"]
            val = decl.get(name)
            if val in (None, ""):
                continue
            sel = f"#linked_{name}"
            self.page.locator(sel).wait_for(state="visible", timeout=wait_linked)
            if f.get("kind") == "select":
                self._select_with_label_fallback(sel, str(val))
            else:
                self._fill(sel, val)
        self.screenshot(f"header_filled_{decl['client_ref']}")
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            self.page.click("#save-header")
        self._guard_login(decl, lambda: self.fill_header(decl, wait_linked))
        self.log("info", "header_saved", "表头已保存")

    def _wait_linked_fields(self, mode: str, timeout: int) -> list[dict[str, Any]]:
        assert self.page is not None
        from declarant.common import trade_mode_fields
        expected = trade_mode_fields(mode)
        if not expected:
            return []
        self.page.wait_for_function(
            """(names) => names.every(n => document.querySelector('#linked_' + n))""",
            arg=[f["name"] for f in expected], timeout=timeout)
        # also wait until the panel lost its 'hidden' class
        self.page.wait_for_selector("#linked-panel:not(.hidden)", timeout=timeout)
        return expected

    # ------------------------------------------------------------------
    # items
    # ------------------------------------------------------------------
    def fill_items(self, decl: dict[str, Any], start_seq: int = 1,
                   on_item_done: Callable[[int], None] | None = None,
                   force: bool = False) -> None:
        """Fill goods lines page by page, starting at start_seq (1-based).

        If the session expires mid-line, the worker logs back in and retries the
        *same* line. Resumption after a crash uses start_seq from the job payload.
        """
        assert self.page is not None
        mock_id = str(decl["mock_decl_id"])
        page_size = 3
        items = decl["items"]
        first_page = max(1, (start_seq - 1) // page_size + 1)
        last_page = max(1, (len(items) - 1) // page_size + 1)
        for pageno in range(first_page, last_page + 1):
            for seq in range((pageno - 1) * page_size + 1,
                             min(pageno * page_size, len(items)) + 1):
                if seq < start_seq:
                    continue
                item = items[seq - 1]
                attempts = 0
                while True:
                    attempts += 1
                    self._open_items_page(mock_id, pageno)
                    if not force and self._item_already_saved(seq, item):
                        self.log("info", "item_skip", f"第 {seq} 项已存在且一致，跳过")
                        break
                    try:
                        self._fill_item(seq, item, decl)
                        break
                    except SessionExpired:
                        if attempts >= 3:
                            raise StepFailed("session", f"第 {seq} 项多次重登后仍未保存")
                        self.log("warn", "item_retry",
                                 f"会话中断，重新登录后重试第 {seq} 项")
                        self.login()
                        self.renewals += 1
                        continue
                if on_item_done:
                    on_item_done(seq)
                self.screenshot(f"item_{seq}_saved")
        self._open_items_page(mock_id, 1)

    def _open_items_page(self, mock_id: str, pageno: int) -> None:
        assert self.page is not None
        self.page.goto(f"{self.base_url}/declarations/{mock_id}/items?page={pageno}",
                       wait_until="domcontentloaded")
        if self._on_login_page():
            self.login()
            self.page.goto(f"{self.base_url}/declarations/{mock_id}/items?page={pageno}",
                           wait_until="domcontentloaded")
            if self._on_login_page():
                raise StepFailed("login", "重新登录失败")

    def _item_already_saved(self, seq: int, item: dict[str, Any]) -> bool:
        assert self.page is not None
        hs = self.page.locator(f"#hs_code_{seq}")
        if hs.count() == 0:
            return False
        cur_hs = normalize_hs_code(hs.input_value())
        if cur_hs != normalize_hs_code(item.get("hs_code", "")):
            return False
        for field, expected in (("quantity", item.get("quantity")),
                                ("total_price", item.get("total_price"))):
            loc = self.page.locator(f"#{field}_{seq}")
            if loc.count() == 0:
                return False
            try:
                current = float(loc.input_value() or 0)
                target = float(expected or 0)
            except ValueError:
                return False
            if abs(current - target) > 0.011:
                return False
        name = self.page.locator(f"#goods_name_{seq}")
        if name.count() and (item.get("goods_name") or "").strip() != name.input_value().strip():
            return False
        return True

    def _fill_item(self, seq: int, item: dict[str, Any], decl: dict[str, Any]) -> None:
        assert self.page is not None
        self._fill(f"#hs_code_{seq}", item["hs_code"])
        # element fields appear asynchronously after HS is accepted
        entry = hs_entry(item["hs_code"])
        if entry:
            try:
                self.page.wait_for_selector(f"#element_{seq}_{entry['elements'][0]['key']}",
                                            timeout=20_000)
            except PWTimeout:
                raise StepFailed("elements", f"第 {seq} 项申报要素未加载（HS {item['hs_code']}）",
                                 self.screenshot(f"item_{seq}_elements_timeout"))
        self._fill(f"#goods_name_{seq}", item.get("goods_name", ""))
        if item.get("specification") is not None:
            self._fill(f"#specification_{seq}", item.get("specification", ""))
        if item.get("brand") is not None:
            self._fill(f"#brand_{seq}", item.get("brand", ""))
        if item.get("model") is not None:
            self._fill(f"#model_{seq}", item.get("model", ""))
        self._fill(f"#quantity_{seq}", _fmt_num(item.get("quantity")))
        self._fill(f"#unit_{seq}", item.get("unit") or "")
        self._fill(f"#legal_quantity_{seq}", _fmt_num(item.get("legal_quantity")))
        self._fill(f"#legal_unit_{seq}", item.get("legal_unit") or "")
        if item.get("second_quantity") is not None:
            self._fill(f"#second_quantity_{seq}", _fmt_num(item.get("second_quantity")))
        if item.get("second_unit"):
            self._fill(f"#second_unit_{seq}", item["second_unit"])
        self._fill(f"#unit_price_{seq}", _fmt_num(item.get("unit_price")))
        self._fill(f"#total_price_{seq}", _fmt_num(item.get("total_price")))
        self._fill(f"#origin_country_{seq}", item.get("origin_country") or "")
        # declaration elements
        for spec in (entry or {}).get("elements", []):
            sel = f"#element_{seq}_{spec['key']}"
            if self.page.locator(sel).count() == 0:
                continue
            val = (item.get("elements") or {}).get(spec["key"], "")
            if val in (None, ""):
                continue
            if spec.get("kind") == "select":
                self._select_with_label_fallback(sel, str(val))
            else:
                self._fill(sel, val)
        self.screenshot(f"item_{seq}_filled")
        self._save_item(seq)
        if self._on_login_page():
            raise SessionExpired(f"保存第 {seq} 项时会话过期")
        if "已保存" not in self.page.content():
            raise StepFailed("item_save", f"第 {seq} 项保存失败",
                             self.screenshot(f"item_{seq}_save_failed"))
        self.log("info", "item_saved", f"第 {seq} 项已录入（{item.get('goods_name')}）")

    def _save_item(self, seq: int) -> None:
        """Click the per-line save button. Split out so tests can straddle it."""
        assert self.page is not None
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            self.page.click(f"#save-item-{seq}")

    # ------------------------------------------------------------------
    # submit + read back
    # ------------------------------------------------------------------
    def submit(self, decl: dict[str, Any]) -> dict[str, Any]:
        """Submit at most once, then verify against the system.

        Safety rule: after the submit button has been clicked in this call, the
        driver never clicks it again. If the result is unclear (session bounce,
        crash page), it polls the customs system by client_ref until the status
        changes. That is what prevents a second submission after a kill -9.
        """
        assert self.page is not None
        mock_id = str(decl["mock_decl_id"])
        ref = decl["client_ref"]
        existing = self.lookup(ref)
        if existing:
            status = existing[0]["status"]
            if status in ("approved", "submitted"):
                self.log("warn", "submit_prevented",
                         f"系统查询到该单已是 {existing[0]['status_text']}，跳过提交（防重复申报）")
                self.screenshot(f"submit_skip_{ref}")
                return {"outcome": status, "decl_no": existing[0]["decl_no"],
                        "reason": "", "prevented_duplicate": True, "code": ""}
        self.page.goto(f"{self.base_url}/declarations/{mock_id}/submit",
                       wait_until="domcontentloaded")
        if self._on_login_page():
            self.login()
            self.page.goto(f"{self.base_url}/declarations/{mock_id}/submit",
                           wait_until="domcontentloaded")
        content = self.page.content()
        if "申报前校验未通过" in content:
            errs = self.page.locator(".error-list li").all_inner_texts()
            raise StepFailed("submit_validate", "系统校验未通过：" + "；".join(errs),
                             self.screenshot(f"submit_validate_{ref}"))
        if self.page.locator("#submit-decl").count() == 0:
            self.log("warn", "submit_button_missing", "提交按钮不存在，可能已提交，改为回读")
            return self._wait_for_status(ref, mock_id)
        self.screenshot(f"submit_before_{ref}")
        with self.page.expect_navigation(wait_until="domcontentloaded"):
            self.page.click("#submit-decl")
        self.screenshot(f"submit_after_{ref}")
        if self._on_login_page():
            self.log("warn", "submit_session_lost",
                     "提交后会话中断，改为向系统核实结果（不再重复提交）")
            self.login()
            return self._wait_for_status(ref, mock_id)
        result = self._read_submit_result()
        if result.get("outcome") == "unknown":
            self.log("warn", "submit_result_unknown", "提交结果未识别，向系统核实")
            return self._wait_for_status(ref, mock_id)
        self.log("info", "submit_result",
                 {"approved": "审核通过", "rejected": "退单"}.get(
                     result.get("outcome"), str(result.get("outcome"))))
        return result

    def _wait_for_status(self, client_ref: str, mock_id: str,
                         timeout: float = 30.0) -> dict[str, Any]:
        deadline = time.time() + timeout
        last = None
        while time.time() < deadline:
            rows = self.lookup(client_ref)
            if rows:
                last = rows[0]
                if last["status"] in ("approved", "rejected", "submitted"):
                    self.log("info", "submit_verified",
                             f"系统核实状态：{last['status_text']}")
                    return {"outcome": last["status"], "decl_no": last["decl_no"],
                            "reason": "", "prevented_duplicate": True, "code": ""}
            time.sleep(1.0)
        raise StepFailed("submit_uncertain",
                         "提交后 30 秒内未能确认系统状态，已停止且不会重复提交，请人工核实",
                         self.screenshot(f"submit_uncertain_{client_ref}"))

    def _read_submit_result(self) -> dict[str, Any]:
        assert self.page is not None
        try:
            self.page.wait_for_selector("#submit-result", timeout=20_000)
        except PWTimeout:
            return {"outcome": "unknown", "decl_no": "", "reason": "提交结果区块未出现",
                    "prevented_duplicate": False, "code": ""}
        text = self.page.locator("#submit-result").inner_text()
        decl_no = ""
        if self.page.locator("#result-decl-no").count():
            decl_no = self.page.locator("#result-decl-no").inner_text().strip()
        reason = ""
        if self.page.locator("#reject-reason").count():
            reason = self.page.locator("#reject-reason").inner_text().strip()
        if "审核通过" in text:
            return {"outcome": "approved", "decl_no": decl_no, "reason": "",
                    "prevented_duplicate": False, "code": ""}
        if "退单" in text:
            code = ""
            m = re.match(r"\[([^\]]+)\]", reason)
            if m:
                code = m.group(1)
            return {"outcome": "rejected", "decl_no": decl_no, "reason": reason,
                    "prevented_duplicate": False, "code": code}
        if "重复申报" in text:
            return {"outcome": "duplicate_blocked", "decl_no": decl_no, "reason": text,
                    "prevented_duplicate": True, "code": "DUPLICATE"}
        return {"outcome": "unknown", "decl_no": decl_no, "reason": text,
                "prevented_duplicate": False, "code": ""}

    def open_declaration(self, mock_id: str) -> None:
        assert self.page is not None
        self.page.goto(f"{self.base_url}/declarations/{mock_id}",
                       wait_until="domcontentloaded")
        if self._on_login_page():
            self.login()
            self.page.goto(f"{self.base_url}/declarations/{mock_id}",
                           wait_until="domcontentloaded")

    def goto_declaration(self, mock_id: str) -> None:
        self.open_declaration(mock_id)

    def read_back(self, mock_id: str) -> dict[str, Any]:
        """Read the declaration back from the system UI into a plain dict."""
        assert self.page is not None
        self.open_declaration(mock_id)
        self.screenshot(f"readback_{mock_id}")
        summary: dict[str, str] = {}
        for div in self.page.locator(".kv-grid > div").all():
            try:
                label = div.locator("span").first.inner_text().strip()
                value = div.locator("b").first.inner_text().strip()
            except Exception:
                continue
            if label and label not in summary:
                summary[label] = value
        header: dict[str, str] = {}
        for div in self.page.locator(".card div[data-field]").all():
            field = div.get_attribute("data-field") or ""
            if not field or div.locator("b").count() == 0:
                continue
            header[field] = div.locator("b").first.inner_text().strip()
        items: list[dict[str, Any]] = []
        for tr in self.page.locator("table.grid tbody tr").all():
            cells = tr.locator("td[data-field]").all()
            if not cells:
                continue
            row: dict[str, Any] = {"elements": {}}
            for td in cells:
                field = td.get_attribute("data-field") or ""
                if field == "elements":
                    for span in td.locator(".el-tag").all():
                        key = span.get_attribute("data-el-key") or ""
                        raw = span.inner_text().strip()
                        row["elements"][key] = raw.split("=", 1)[1] if "=" in raw else raw
                elif field in ("quantity", "legal_quantity"):
                    unit = td.get_attribute("data-unit") or ""
                    raw = td.inner_text().strip()
                    row[field] = raw.replace(unit, "").strip() if unit else raw
                    row[field + "_unit"] = unit
                else:
                    row[field] = td.inner_text().strip()
            if row.get("hs_code"):
                items.append(row)
        items.sort(key=lambda r: int(r.get("seq_no") or 0))
        return {"summary": summary, "header": header, "items": items,
                "decl_no": summary.get("报关单号", ""),
                "status_text": summary.get("状态", ""),
                "client_ref": summary.get("客户参考号", ""),
                "raw_text": (self.page.content() or "")[:4000]}

    def read_status(self, client_ref: str) -> str | None:
        rows = self.lookup(client_ref)
        return rows[0]["status"] if rows else None

    # ------------------------------------------------------------------
    # session guard
    # ------------------------------------------------------------------
    def _guard_login(self, decl: dict[str, Any], replay: Callable[[], Any] | None) -> bool:
        """If bounced to the login page, log back in and replay the current step."""
        if not self._on_login_page():
            return False
        self.log("warn", "session_expired", "会话已过期，正在自动重新登录")
        self.login()
        self.renewals += 1
        self.log("info", "session_renewed", "已重新登录，继续当前步骤")
        if replay:
            replay()
        return replay is not None

    def probe_session(self) -> dict[str, Any]:
        """Check that the session is still valid, logging in again if needed."""
        assert self.page is not None
        if self._on_login_page():
            self.login()
            return {"renewed": True, "expires_at": self.current_session_expiry()}
        try:
            self.page.goto(f"{self.base_url}/", wait_until="domcontentloaded")
        except Exception:
            pass
        if self._on_login_page():
            self.login()
            return {"renewed": True, "expires_at": self.current_session_expiry()}
        return {"renewed": False, "expires_at": self.current_session_expiry()}


def _fmt_num(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))
    return str(value)
