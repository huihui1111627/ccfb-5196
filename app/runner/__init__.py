"""Entry runner.

The loop, for each document:

1. **Recovery**: check the portal for evidence of a prior submit (external ref
   query).  If a declaration exists we settle it as accepted, never submit
   again.  If the portal knows a definitive rejection we move on.  If the fence
   says `submitting` from a dead process and the portal has no evidence, we mark
   it `uncertain` and skip — an operator resolves it.
2. **Draft**: open the new-declaration page and resume from the portal's own
   saved draft.  Items already 暂存 are skipped; entry continues at the first
   missing item number.
3. **Fence**: claim the submission *inside a committed transaction*.  Only after
   that does the submit POST happen.
4. **Submit**: click 提交, parse the result page.
5. **Settle**: accepted -> declaration_no stored; rejected -> rejection queue;
   timeout/ambiguous -> `uncertain` (never retried automatically).
6. **Verify**: read the declaration back from the portal and diff every field.

Session expiry is detected at any step; `SessionExpired` re-logs in and the
loop repeats the current item from the *portal draft*, so no line is lost and no
line is submitted twice.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone

from app.db import Database, now_iso
from app.runner.portal import MockPortal, PortalError, SessionExpired, SubmitNotSent
from app.services import fence
from app.services.reconcile import verify_document
from app.services.state import (
    ENTRY_ELIGIBLE, IllegalTransition, log_event, set_document_state,
)

STOP_EVENT = threading.Event()

RESUME_COMPARE_FIELDS = ("hs_code", "goods_name", "spec_model", "quantity", "unit",
                         "unit_price", "total_price", "currency", "origin_country",
                         "destination_country")


def _item_matches(local: dict, portal_item: dict) -> bool:
    """Does the portal's saved draft line still equal the source document line?"""
    from common.formatting import parse_elements, values_equal

    for field in RESUME_COMPARE_FIELDS:
        if not values_equal(field, local.get(field, ""), portal_item.get(field, "")):
            return False
    if parse_elements(local.get("elements") or {}) != parse_elements(
            portal_item.get("elements") or {}):
        return False
    return True


@dataclass
class RunConfig:
    batch_id: int | None = None
    document_ids: list[int] | None = None
    headless: bool = True
    slow_mo_ms: int = 0
    max_resubmit: int = 3
    verify_after_submit: bool = True
    settle_seconds_after_uncertain: float = 2.0
    probe_attempts: int = 2
    steps_to_shot: set[str] = field(default_factory=lambda: {
        "login", "header", "item", "submit", "result", "verify", "recovery", "error",
    })


class Runner:
    def __init__(self, db: Database, portal: MockPortal, config: RunConfig,
                 settings: dict, run_id: int | None = None):
        self.db = db
        self.portal = portal
        self.config = config
        self.settings = settings
        self.run_id = run_id
        self.claimant = fence.claimant_id("runner")
        self.stats = {
            "docs_attempted": 0, "accepted": 0, "rejected": 0,
            "skipped_accepted": 0, "uncertain": 0, "failed": 0,
            "verified_ok": 0, "verified_mismatch": 0, "relogins": 0,
            "items_entered": 0, "started_at": now_iso(),
        }
        self.stop_requested = False

    # -- helpers ------------------------------------------------------------
    def _log(self, doc_id, step, status, message="", item_no=None, shot=""):
        url = self.portal.url()
        if url in ("about:blank", ""):
            url = None
        log_event(self.db, doc_id, "run", status, message, run_id=self.run_id,
                  item_no=item_no, step=step, screenshot_path=shot, url=url)


    def _shot(self, label: str, step: str) -> str:
        if step not in self.config.steps_to_shot:
            return ""
        path = self.portal.shot(label)
        return path

    def _crash_if_requested(self, doc_id: int, point_name: str) -> None:
        """Test hook: RUNNER_CRASH_POINT names the step to die at (os._exit)."""
        point = os.environ.get("RUNNER_CRASH_POINT", "")
        target_doc = os.environ.get("RUNNER_CRASH_DOC", "")
        if not point or point != point_name:
            return
        if target_doc and str(doc_id) != target_doc:
            return
        os._exit(99)

    def _heartbeat(self, status: str = "running", stats=None):
        if self.run_id:
            self.db.execute(
                "UPDATE runs SET heartbeat_at=?, status=?, stats_json=? WHERE id=?",
                (now_iso(), status, json.dumps(stats or self.stats, ensure_ascii=False),
                 self.run_id),
            )
        current = self.db.get_settings().get("runner_active", "")
        if current.startswith("web:"):
            # The web console tracks its child pid here; don't overwrite it.
            return
        self.db.set_setting("runner_active", f"{self.claimant}|{now_iso()}")

    # -- portal session -----------------------------------------------------
    def _with_session(self, action, *args, **kwargs):
        """Run `action`, re-login once on SessionExpired and retry."""
        try:
            return action(*args, **kwargs)
        except SessionExpired:
            self._log(None, "session", "warning",
                      "会话过期，自动重新登录并从中断处继续")
            self.portal.relogin_count += 1
            self.stats["relogins"] += 1
            try:
                self.portal.login()
            except Exception as exc:  # noqa: BLE001
                raise PortalError(f"自动重登失败：{exc}") from exc
            return action(*args, **kwargs)

    # -- recovery -----------------------------------------------------------
    def _recover_document(self, doc_id: int) -> str:
        """Determine what happened to a possibly-submitted document.

        Returns one of: "accepted", "rejected", "none", "uncertain".
        """
        sub = self.db.one("SELECT * FROM submissions WHERE doc_id=?", (doc_id,))
        state = sub["state"] if sub else fence.ST_NONE
        if state == fence.ST_ACCEPTED:
            return "accepted"
        if state == fence.ST_NONE:
            return "none"
        if state == fence.ST_REJECTED:
            return "rejected"
        # submitting / uncertain -> ask the portal.
        doc = self.db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
        evidence = None
        last_error = ""
        for attempt in range(max(1, self.config.probe_attempts)):
            try:
                evidence = self._with_session(self.portal.probe_external_ref,
                                              doc["external_ref"])
                break
            except (PortalError, Exception) as exc:  # noqa: BLE001
                last_error = str(exc)
                time.sleep(self.config.settle_seconds_after_uncertain)
        shot = self._shot(f"recovery_{doc['external_ref']}", "recovery")
        if not evidence:
            fence.settle_uncertain(self.db, doc_id, self.claimant,
                                   f"恢复时无法查询系统：{last_error}")
            self._log(doc_id, "recovery", "error",
                      f"无法查询系统确认提交结果：{last_error}", shot=shot)
            return "uncertain"

        if evidence["accepted"]:
            declaration_no = evidence["declaration_no"]
            fence.settle_accepted(self.db, doc_id, self.claimant, declaration_no,
                                  detail="崩溃恢复：系统已有受理记录")
            if self._status(doc_id) in ("submitting", "queued"):
                self._set_state_safe(doc_id, "submitted", "恢复确认已申报")
            self._log(doc_id, "recovery", "ok",
                      f"恢复发现系统已有申报单 {declaration_no}，不再重录", shot=shot)
            return "accepted"
        if evidence["last_outcome"] == "rejected":
            fence.settle_rejected(
                self.db, doc_id, self.claimant,
                evidence.get("last_reason_code") or "UNKNOWN",
                evidence.get("last_reason_message") or "系统退回",
                category="recovery", hint="由恢复流程记录的退回",
            )
            self._log(doc_id, "recovery", "warning",
                      "恢复发现系统已有退单记录", shot=shot)
            return "rejected"
        # Neither accepted nor rejected: the submit might have been in flight.
        if state == fence.ST_UNCERTAIN:
            return "uncertain"
        fence.settle_uncertain(
            self.db, doc_id, self.claimant,
            "提交后进程中断，系统未查到受理或退单记录")
        self._log(doc_id, "recovery", "error",
                  "提交结果不确定：系统无记录，已锁定该票待人工核实（不会自动重交）",
                  shot=shot)
        return "uncertain"

    def _status(self, doc_id: int) -> str:
        row = self.db.one("SELECT status FROM documents WHERE id=?", (doc_id,))
        return row["status"] if row else ""

    # -- document entry -----------------------------------------------------
    def enter_document(self, doc_id: int) -> str:
        doc = self.db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
        if not doc:
            self.stats["failed"] += 1
            return "missing"
        ref = doc["external_ref"]

        # An operator-explicit re-entry of a previously rejected ticket is not
        # a recovery case: the rejection was definitive, the data has been
        # fixed, and the fence has no uncertainty.  Only run recovery when the
        # previous attempt could still be in flight.
        prior_state = fence.get_state(self.db, doc_id)
        if prior_state == fence.ST_REJECTED and self._status(doc_id) in (
                "queued", "revision", "ready"):
            recovery = "none"
        else:
            recovery = self._recover_document(doc_id)
        if recovery == "accepted":
            self.stats["skipped_accepted"] += 1
            self._finish_after_accepted(doc_id)
            return "skipped"
        if recovery == "uncertain":
            self.stats["uncertain"] += 1
            self._set_state_safe(doc_id, "failed",
                                 "提交结果不确定，待人工核实（禁止自动重交）")
            return "uncertain"
        if recovery == "rejected":
            self.stats["rejected"] += 1
            self._set_state_safe(doc_id, "rejected", "系统退回")
            return "rejected"

        header = json.loads(doc["header_json"])
        items = json.loads(doc["items_json"])
        if self._status(doc_id) not in ENTRY_ELIGIBLE:
            return "skipped"
        self._set_state_safe(doc_id, "queued", "runner 开始录入")
        self.stats["docs_attempted"] += 1

        # -- 0. portal preflight -------------------------------------------
        # The local fence can be behind reality (DB restored from a backup, an
        # operator's manual submission, a previous process that died before
        # writing).  Always ask the portal whether this external ref already
        # exists before touching the form.  Two accepted declarations for the
        # same 票 is the failure this system exists to prevent.
        try:
            pre = self._with_session(self.portal.probe_external_ref, ref)
        except Exception as exc:  # noqa: BLE001
            return self._fail(doc_id, f"录入前查询系统失败（未做任何提交）：{exc}")
        existing = pre.get("declarations") or []
        if existing:
            declaration_no = existing[0]["declaration_no"]
            fence.settle_accepted(self.db, doc_id, self.claimant, declaration_no,
                                  detail="录入前发现系统已存在该票申报单，跳过")
            self.stats["skipped_accepted"] += 1
            self._log(doc_id, "preflight", "warning",
                      f"系统中已存在该外部单号（{declaration_no}，共 "
                      f"{len(existing)} 条记录），本次不再录入")
            self._set_state_safe(doc_id, "submitted", "系统已存在，跳过")
            self._finish_after_accepted(doc_id)
            return "skipped"

        # -- 1. open page, resume portal draft ------------------------------
        try:
            self._open_declaration_with_relogin(doc_id, ref)
            draft = self.portal.load_draft_or_blank(ref)
        except Exception as exc:  # noqa: BLE001
            return self._fail_safe_no_submit(doc_id, f"打开申报页失败：{exc}")
        saved_items = {}
        stale_items = []
        if draft and draft.get("exists") and not draft.get("submitted"):
            for key, saved in (draft.get("items") or {}).items():
                item_no = int(key)
                local = next((it for it in items if int(it["item_no"]) == item_no), None)
                if local is not None and _item_matches(local, saved):
                    saved_items[item_no] = saved
                else:
                    # The portal copy is stale (operator fixed the source data
                    # after the crash).  Re-enter it rather than touching a
                    # draft whose content is no longer the source of truth.
                    stale_items.append(item_no)
        if saved_items:
            self._log(doc_id, "resume", "info",
                      f"发现系统草稿，已暂存且与源单一致项号 {sorted(saved_items)}，从中断处继续")
        if stale_items:
            self._log(doc_id, "resume", "warning",
                      f"系统草稿中项号 {sorted(stale_items)} 与源单不一致（可能已人工修正），"
                      f"将重新录入这些明细")
        self._shot(f"{ref}_open", "header")

        # -- 2. header ------------------------------------------------------
        try:
            self._with_session(self.portal.fill_header, header, header.get("trade_mode", ""))
            self._with_session(self.portal.save_header)
            shot = self._shot(f"{ref}_header", "header")
            self._log(doc_id, "header", "ok",
                      f"表头已录入（贸易方式 {header.get('trade_mode')}）", shot=shot)
        except Exception as exc:  # noqa: BLE001
            return self._fail_safe_no_submit(doc_id, f"录入表头失败：{exc}")

        # -- 3. items, page by page ----------------------------------------
        for item in items:
            item_no = int(item["item_no"])
            if item_no in saved_items:
                continue
            expire_after = int(os.environ.get("RUNNER_EXPIRE_AFTER_ITEM", "0") or 0)
            if expire_after and item_no == expire_after:
                self._log(doc_id, "session", "warning",
                          f"测试：第{item_no}项后强制会话过期", item_no=item_no)
                try:
                    self.portal.debug_expire_sessions()
                except Exception:  # noqa: BLE001
                    pass
            try:
                result = self._enter_item_with_retry(ref, doc_id, item, item_no)
            except Exception as exc:  # noqa: BLE001
                return self._fail_safe_no_submit(doc_id, f"录入第{item_no}项失败：{exc}")
            if result == "session_lost":
                return self._fail_safe_no_submit(doc_id,
                                                 f"第{item_no}项会话中断，下次运行继续")
            self.stats["items_entered"] += 1

        # -- 4. fence claim ------------------------------------------------
        sha = fence.payload_sha(header, items)
        try:
            claim = fence.acquire_claim(self.db, doc_id, self.claimant, sha,
                                        self.config.max_resubmit)
        except fence.FenceError as exc:
            self._log(doc_id, "fence", "error", f"拒绝提交：{exc}")
            if "已受理" in str(exc) or "已处于" in str(exc):
                self.stats["skipped_accepted"] += 1
                self._finish_after_accepted(doc_id)
            else:
                self.stats["uncertain"] += 1
                self._set_state_safe(doc_id, "failed", str(exc))
            return "fence_blocked"
        fence.record_draft(self.db, doc_id, self.claimant)
        self._set_state_safe(doc_id, "submitting", "已取得提交许可")
        self._log(doc_id, "fence", "info",
                  f"已取得提交许可（第{claim.attempt_seq}次），准备提交")
        self._shot(f"{ref}_before_submit", "submit")
        self._heartbeat(stats=self.stats)

        # The kill window that matters: after claim, before POST.  Recovery must
        # treat this as uncertain and never auto-resubmit.
        self._crash_if_requested(doc_id, "after_claim")

        # -- 5. submit -----------------------------------------------------
        self._log(doc_id, "submit", "info", "点击提交申报（此步骤后不可撤销）")
        try:
            outcome = self._with_session(self.portal.submit)
        except SubmitNotSent:
            # Explicit server-side evidence that nothing was committed (the
            # request was bounced to the login page).  Safe to release the claim
            # so the ticket can be entered again in a later run.
            fence.release_unsubmitted(self.db, doc_id, self.claimant,
                                      "提交前会话过期，服务器未处理请求")
            self._log(doc_id, "submit", "warning",
                      "提交前会话过期，服务器确认未提交，可下次重录")
            self._set_state_safe(doc_id, "queued", "会话过期，待重录")
            return "session_expired"
        except Exception as exc:  # noqa: BLE001
            # Ambiguous: network error / timeout / page crash.  Never retry.
            time.sleep(self.config.settle_seconds_after_uncertain)
            try:
                self._recover_after_ambiguous(doc_id, ref)
            except Exception:  # noqa: BLE001
                pass
            state = fence.get_state(self.db, doc_id)
            if state == fence.ST_ACCEPTED:
                self.stats["accepted"] += 1
                self._finish_after_accepted(doc_id)
                return "accepted"
            if state == fence.ST_REJECTED:
                self.stats["rejected"] += 1
                self._set_state_safe(doc_id, "rejected", "系统退回")
                return "rejected"
            self.stats["uncertain"] += 1
            # Record the uncertainty explicitly so the console shows the
            # "结果不明" locked state (the fence stays non-reclaimable either way).
            try:
                fence.settle_uncertain(self.db, doc_id, self.claimant,
                                       f"提交异常且系统未查到结果：{exc}")
            except Exception:  # noqa: BLE001
                pass
            self._log(doc_id, "submit", "error",
                      f"提交异常：{exc}；结果待核实，不会自动重交")
            self._set_state_safe(doc_id, "failed", "提交结果不确定，待人工核实")
            return "uncertain"

        # The POST has returned: the server has committed, but our local settle
        # transaction has not run yet.  A kill here is the classic "server
        # accepted, client unsure" case; recovery must find it by querying.
        self._crash_if_requested(doc_id, "after_submit_response")

        shot = self._shot(f"{ref}_result", "result")
        if outcome.get("kind") == "accepted":
            declaration_no = outcome.get("declaration_no", "")
            fence.settle_accepted(self.db, doc_id, self.claimant, declaration_no,
                                  detail="提交返回受理")
            self._set_state_safe(doc_id, "submitted", "系统受理")
            self.stats["accepted"] += 1
            self._log(doc_id, "submit", "ok", f"申报成功：{declaration_no}", shot=shot)
            self._crash_if_requested(doc_id, "after_accept_settle")
            self._finish_after_accepted(doc_id)
            return "accepted"
        if outcome.get("kind") == "rejected":
            code = outcome.get("reason_code", "") or "UNKNOWN"
            reason = outcome.get("reason_text", "") or "系统退回"
            hint = outcome.get("hint", "")
            category = self._parse_rejection_category(reason, hint)
            fence.settle_rejected(self.db, doc_id, self.claimant, code, reason,
                                  category=category, hint=hint, detail=reason)
            self._set_state_safe(doc_id, "rejected", f"退单 {code}: {reason}")
            self.stats["rejected"] += 1
            self._log(doc_id, "submit", "warning", f"系统退单 {code}：{reason}", shot=shot)
            return "rejected"

        # Ambiguous result page: treat as uncertain.
        fence.settle_uncertain(self.db, doc_id, self.claimant,
                               f"结果页面无法解析：{outcome.get('raw', '')[:200]}")
        self.stats["uncertain"] += 1
        self._set_state_safe(doc_id, "failed", "提交结果无法解析，待人工核实")
        self._log(doc_id, "submit", "error", "提交结果页无法解析", shot=shot)
        return "uncertain"

    def _open_declaration_with_relogin(self, doc_id: int, ref: str) -> None:
        try:
            self.portal.open_new_declaration(ref)
        except SessionExpired:
            self.stats["relogins"] += 1
            self._log(doc_id, "session", "warning",
                      "打开申报页时会话过期，自动重新登录后继续")
            self.portal.login()
            self.portal.open_new_declaration(ref)

    def _enter_item_with_retry(self, ref: str, doc_id: int, item: dict, item_no: int):
        for attempt in range(2):
            try:
                fill_result = self.portal.fill_item(item, item_no)
                save = self.portal.save_item(item_no)
                shot = self._shot(f"{ref}_item_{item_no}", "item")
                msg = f"第{item_no}项已暂存"
                if fill_result.get("missing_elements"):
                    msg += f"（要素未填：{', '.join(fill_result['missing_elements'])}）"
                    self._log(doc_id, "item", "warning", msg, item_no=item_no, shot=shot)
                else:
                    self._log(doc_id, "item", "ok", msg, item_no=item_no, shot=shot)
                return "ok"
            except SessionExpired:
                if attempt == 0:
                    self.portal.relogin_count += 1
                    self.stats["relogins"] += 1
                    shot = self._shot(f"{ref}_relogin", "login")
                    self.portal.login()
                    self.stats["session_expired_events"] = self.stats.get(
                        "session_expired_events", 0) + 1
                    # Reopen the declaration page *and* continue from the draft.
                    self.portal.open_new_declaration(ref)
                    draft = self.portal.load_draft_or_blank(ref)
                    saved = sorted(int(k) for k in ((draft or {}).get("items") or {}))
                    self._log(doc_id, "session", "warning",
                              f"会话过期→已自动重登（第{doc_id}票）。系统草稿已含项号 {saved}，"
                              f"从第{item_no}项继续录入", item_no=item_no, shot=shot)
                    continue
                return "session_lost"
        return "session_lost"

    def _recover_after_ambiguous(self, doc_id: int, ref: str) -> None:
        doc = self.db.one("SELECT * FROM documents WHERE id=?", (doc_id,))
        evidence = self._with_session(self.portal.probe_external_ref, ref)
        if evidence["accepted"]:
            fence.settle_accepted(self.db, doc_id, self.claimant,
                                  evidence["declaration_no"],
                                  detail="异常恢复：系统已有受理记录")
            return
        if evidence["last_outcome"] == "rejected":
            fence.settle_rejected(
                self.db, doc_id, self.claimant,
                evidence.get("last_reason_code") or "UNKNOWN",
                evidence.get("last_reason_message") or "系统退回",
                category="recovery", hint="异常恢复时发现的退回")

    def _finish_after_accepted(self, doc_id: int) -> None:
        sub = self.db.one("SELECT * FROM submissions WHERE doc_id=?", (doc_id,))
        if self._status(doc_id) == "submitted":
            pass
        elif self._status(doc_id) in ("queued", "submitting", "ready", "revision", "failed"):
            self._set_state_safe(doc_id, "submitted", "已申报")
        if not self.config.verify_after_submit or not sub or not sub["declaration_no"]:
            return
        try:
            result = self._with_session(verify_document, self.db, doc_id, self.portal,
                                        self.run_id)
        except Exception as exc:  # noqa: BLE001
            self._log(doc_id, "verify", "warning", f"回读核对失败：{exc}")
            return
        shot = self._shot(f"{sub['declaration_no']}_readback", "verify")
        if result["matched"]:
            self._set_state_safe(doc_id, "verified", "回读一致")
            self.stats["verified_ok"] += 1
            self._log(doc_id, "verify", "ok",
                      f"回读核对一致（{result['checked_fields']} 字段）", shot=shot)
        else:
            self._set_state_safe(doc_id, "verified_mismatch", "回读存在差异")
            self.stats["verified_mismatch"] += 1
            self._log(doc_id, "verify", "error",
                      f"回读发现 {len(result['differences'])} 处差异", shot=shot)

    def _fail(self, doc_id: int, message: str) -> str:
        self.stats["failed"] += 1
        self._set_state_safe(doc_id, "failed", message[:300])
        self._log(doc_id, "error", "error", message, shot=self._shot("error", "error"))
        return "failed"

    def _fail_safe_no_submit(self, doc_id: int, message: str) -> str:
        """Entry was interrupted before any submit claim: safe to retry later."""
        self.stats["failed"] += 1
        self._set_state_safe(doc_id, "queued", message[:300])
        self._log(doc_id, "error", "warning", message)
        return "interrupted"

    def _set_state_safe(self, doc_id: int, state: str, reason: str) -> None:
        try:
            set_document_state(self.db, doc_id, state, reason, allow_same=True)
        except IllegalTransition:
            # Keep the fence authoritative; a status mismatch is logged rather
            # than forced.
            self._log(doc_id, "state", "warning",
                      f"状态无法从 {self._status(doc_id)} 转到 {state}：{reason}")

    @staticmethod
    def _parse_rejection_category(reason: str, hint: str) -> str:
        text = f"{reason} {hint}"
        if "申报要素" in text or "要素" in text:
            return "element"
        if "HS" in text or "编码" in text or "税则" in text:
            return "hs"
        if "单位" in text:
            return "unit"
        if "总价" in text or "单价" in text or "金额" in text:
            return "amount"
        if "贸易方式" in text or "单证" in text:
            return "document"
        if "发货人" in text or "收货人" in text:
            return "party"
        return "other"

    # -- top level ----------------------------------------------------------
    def pick_documents(self) -> list[int]:
        if self.config.document_ids:
            rows = self.db.query(
                "SELECT id FROM documents WHERE id IN (%s) AND status IN "
                "('ready','queued','revision','failed','submitting') ORDER BY sequence_no, id"
                % ",".join("?" for _ in self.config.document_ids),
                tuple(self.config.document_ids),
            )
            return [r["id"] for r in rows]
        if self.config.batch_id:
            rows = self.db.query(
                "SELECT id FROM documents WHERE batch_id=? AND status IN "
                "('ready','queued','revision','failed','submitting') ORDER BY sequence_no, id",
                (self.config.batch_id,),
            )
            return [r["id"] for r in rows]
        rows = self.db.query(
            "SELECT id FROM documents WHERE status IN "
            "('ready','queued','revision','failed','submitting') ORDER BY sequence_no, id")
        return [r["id"] for r in rows]

    def run(self) -> dict:
        # Claim any stale in-flight rows from a dead process *before* entry.
        self._heartbeat()
        from app.runner import reap_stale_runs
        reap_stale_runs(self.db)
        doc_ids = self.pick_documents()
        self._log(None, "run", "info", f"本次待录 {len(doc_ids)} 票")
        try:
            self.portal.login()
            self._shot("login", "login")
            self._log(None, "login", "ok", "已登录申报系统")
        except Exception as exc:  # noqa: BLE001
            # Portal unreachable: nothing has been claimed or submitted, so this
            # is always safe to retry later.  Record and end the run cleanly.
            self._log(None, "login", "error",
                      f"登录失败（未做任何提交，可稍后重试）：{exc}")
            self.stats["login_failed"] = True
            self._finalize("aborted")
            return self.stats
        for doc_id in doc_ids:
            if STOP_EVENT.is_set():
                self._log(None, "run", "warning", "收到停止信号，停止后续录入")
                break
            if self._check_stop_file():
                break
            self.enter_document(doc_id)
            self._heartbeat(stats=self.stats)
        self._finalize("finished")
        return self.stats

    def _check_stop_file(self) -> bool:
        stop_file = os.environ.get("RUNNER_STOP_FILE", "")
        if stop_file and os.path.exists(stop_file):
            return True
        return False

    def _finalize(self, status: str) -> None:
        self.stats["finished_at"] = now_iso()
        if self.run_id:
            self.db.execute(
                "UPDATE runs SET finished_at=?, status=?, stats_json=?, heartbeat_at=? "
                "WHERE id=?",
                (now_iso(), status, json.dumps(self.stats, ensure_ascii=False),
                 now_iso(), self.run_id),
            )
        self.db.execute(
            "INSERT INTO daily_stats(day, submitted, accepted, rejected, mismatched, "
            "items_entered, updated_at) VALUES(?,?,?,?,?,?,?) "
            "ON CONFLICT(day) DO UPDATE SET submitted=submitted+excluded.submitted, "
            "accepted=accepted+excluded.accepted, rejected=rejected+excluded.rejected, "
            "mismatched=mismatched+excluded.mismatched, "
            "items_entered=items_entered+excluded.items_entered, "
            "updated_at=excluded.updated_at",
            (datetime.now(timezone.utc).date().isoformat(),
             self.stats["docs_attempted"], self.stats["accepted"], self.stats["rejected"],
             self.stats["verified_mismatch"], self.stats["items_entered"], now_iso()),
        )
        if not self.db.get_settings().get("runner_active", "").startswith("web:"):
            self.db.set_setting("runner_active", "")
        self._log(None, "run", "ok" if status == "finished" else "warning",
                  f"本次运行结束：{json.dumps(self.stats, ensure_ascii=False)}")


def install_signal_handlers() -> None:
    def _handler(signum, frame):  # noqa: ANN001
        STOP_EVENT.set()
    try:
        signal.signal(signal.SIGINT, _handler)
        signal.signal(signal.SIGTERM, _handler)
    except ValueError:
        pass


def recover_stale_claims(db: Database, older_than_seconds: int = 0) -> list[dict]:
    """At startup: any `submitting` claim left by a dead process is *not*
    released blindly.  It stays `submitting` so the fence refuses re-entry;
    the runner's `_recover_document` queries the portal before deciding.

    Also reaps runs whose owning process died (heartbeat stopped), so the
    console never shows a ghost "运行中" task.
    """
    out = []
    for row in fence.stale_claims(db, older_than_seconds=older_than_seconds):
        doc = db.one("SELECT * FROM documents WHERE id=?", (row["doc_id"],))
        out.append({"doc_id": row["doc_id"],
                    "external_ref": doc["external_ref"] if doc else "",
                    "state": row["state"], "updated_at": row["updated_at"]})
    reaped = reap_stale_runs(db)
    return out + [{"run_reaped": r} for r in reaped]


def reap_stale_runs(db: Database, stale_after_seconds: int = 120) -> list[int]:
    """Mark runs as crashed when their heartbeat has gone stale."""
    from datetime import timedelta

    cutoff = (datetime.now(timezone.utc) - timedelta(seconds=stale_after_seconds)
              ).isoformat(timespec="seconds")
    rows = db.query(
        "SELECT id FROM runs WHERE mode='auto' AND status='running' "
        "AND (heartbeat_at IS NULL OR heartbeat_at < ?)", (cutoff,))
    reaped = []
    for r in rows:
        db.execute(
            "UPDATE runs SET status='crashed_recovered', finished_at=? WHERE id=?",
            (now_iso(), r["id"]))
        log_event(db, None, "run", "warning",
                  f"任务 #{r['id']} 的进程已退出（心跳停止），标记为中断；"
                  f"其提交中的单据将在下次运行时先核实系统", run_id=r["id"])
        reaped.append(r["id"])
    return reaped
