# -*- coding: utf-8 -*-
"""PromptBox 断线修复 · 真实 Tk GUI 点击流验证驱动。

在项目根目录用「系统 Python 3.10」运行：
    python scripts/verify_gui_clickflow.py

作用：创建真实 Tkinter 窗口（RepairWorkbench.open_toplevel），注入 FakeService
（模拟 RepairService 契约，无网络无 AI），通过真实 Button.invoke() 驱动
「一键优化 → 候选 → 运行比较」完整点击流，断言：
  ① 断线同步：验证阶段自动带入优化时填的「本次材料」与「业务背景」；
  ② 命名统一：用户可见标签为「业务背景 / 本次材料」，无旧称「业务上下文 / 触发输入」。

注意：托管 Python 3.13 无 tkinter，必须用系统 Python（含 Tkinter）运行。
FakeService.repair 的 audit / quick_check 必须是 dict（真实 RepairService 形状）。
"""
import sys, io, traceback

sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8")
sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8")

import os
PROJ = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, PROJ)

import tkinter as tk
from tkinter import messagebox as _mb
# 打桩 messagebox：非阻塞，防无头环境卡死（真实应在桌面验证，此处只验逻辑）
_MB_LOGS = []
def _stub(which):
    def _f(*a, **k):
        _MB_LOGS.append((which, a))
        return "ok"
    return _f
_mb.showerror = _stub("showerror")
_mb.showinfo = _stub("showinfo")
_mb.showwarning = _stub("showwarning")
_mb.askyesno = _stub("askyesno")
_mb.askokcancel = _stub("askokcancel")

from promptbox_mvp.workbench import RepairWorkbench


class FakeService(object):
    """模拟 RepairService：无网络、无 AI，满足契约返回。
    audit / quick_check 必须是 dict（真实 repair 形状），否则
    _format_audit_summary 里 audit.get() 会因 list 而 AttributeError。
    """

    def repair(self, prompt, output, comparison_input, task_goal, mode, ui_context_text=""):
        return {
            "diagnosis": "诊断：原提示词存在冗余指令与模糊目标。",
            "mode": mode,
            "candidate": "【候选】" + prompt.strip(),
            "reasons": ["去冗", "结构化", "补边界"],
            "audit": {
                "task": "修复提示词",
                "constraints": ["保留原意"],
                "output_contract": ["输出候选版本"],
                "coverage": {"has_scope_boundary": True, "has_task_object": True, "has_input_boundary": True},
                "issues": [],
            },
            "audit_status": {"status": "available", "message": ""},
            "quick_check": {"required_changes": [], "preserve": [], "forbidden_changes": [], "acceptance_checks": []},
            "resolved_issue_codes": [],
            "unresolved_issue_codes": [],
        }

    def verify(self, candidate_prompt, context_text, user_input, context_label="", max_context_chars=None):
        return {
            "output": f"输出 [材料={user_input[:24]}…] len(context)={len(context_text)}",
            "payload_chars": len(candidate_prompt) + len(context_text),
            "context_chars": len(context_text),
            "truncated": False,
            "elapsed_ms": 42,
        }


RESULTS = {"ok": [], "fail": []}


def check(name, cond, detail=""):
    if cond:
        RESULTS["ok"].append(name)
        print(f"  [PASS] {name}")
    else:
        RESULTS["fail"].append(name)
        print(f"  [FAIL] {name} :: {detail}")


def all_widgets(root):
    out = []
    def walk(w):
        for c in w.winfo_children():
            out.append(c)
            walk(c)
    walk(root)
    return out


def find_buttons(root):
    return [w for w in all_widgets(root) if isinstance(w, tk.Button)]


def find_texts(root):
    return [w for w in all_widgets(root) if isinstance(w, tk.Text)]


def find_label(root, text):
    for w in all_widgets(root):
        if isinstance(w, tk.Label) and (w.cget("text") == text):
            return w
    return None


def text_after_label(root, label_text):
    lbl = find_label(root, label_text)
    if lbl is None:
        return None
    parent = lbl.master
    children = parent.winfo_children()
    idx = children.index(lbl)
    for nxt in children[idx + 1:]:
        if isinstance(nxt, tk.Text):
            return nxt
    return None


def node_state(t):
    try:
        return str(t.cget("state"))
    except Exception:
        return "?"


def run():
    root = tk.Tk()
    root.withdraw()
    svc = FakeService()

    # 模拟从主编辑器打开：预置基线 + 优化阶段存储的业务背景
    wb = RepairWorkbench(service=svc)
    wb.ui_baseline = {"snippet_id": "snip_01", "base_version_id": "v1", "base_version_number": 1}
    wb.ui_baseline_content = "你是电商客服，回答用户关于订单的问题。\n输出简洁。"
    wb.ui_context = "我们是做母婴电商的，目标用户是新手妈妈。"

    window = wb.open_toplevel(root)
    root.update_idletasks(); root.update()

    # ---------- 定位真实控件 ----------
    gen_btn = None
    for b in find_buttons(window):
        if b.cget("text") == "一键优化":
            gen_btn = b
    check("定位「一键优化」按钮", gen_btn is not None)

    prompt_text = text_after_label(window, "原提示词")
    check("定位「原提示词」输入框", prompt_text is not None)

    comp_text = text_after_label(window, "本次材料（可选）")
    check("定位「本次材料（可选）」输入框", comp_text is not None)

    # 业务背景 text：context_frame（含「业务背景」label）下 state=normal 的 Text
    ctx_frame = find_label(window, "业务背景")
    ctx_text = None
    if ctx_frame is not None:
        pf = ctx_frame.master
        for t in find_texts(pf):
            if node_state(t) == "normal":
                ctx_text = t
                break
    check("定位「业务背景」文本区", ctx_text is not None)

    # 验证面板本次材料：label「本次材料（单次代表性任务输入）：」后面的 Text
    verify_input = text_after_label(window, "本次材料（单次代表性任务输入）：")
    check("定位验证面板「本次材料」输入框", verify_input is not None)

    # ---------- 填入优化阶段材料 ----------
    prompt_text.delete("1.0", tk.END); prompt_text.insert("1.0", "你是电商客服，回答用户关于订单的问题。\n输出简洁。")
    comp_text.delete("1.0", tk.END); comp_text.insert("1.0", "订单#20260830-1 显示已签收，但用户没收到货")
    ctx_text.delete("1.0", tk.END); ctx_text.insert("1.0", "我们是做母婴电商的，目标用户是新手妈妈，客服要求有耐心。")
    root.update_idletasks()

    # ---------- 点击「一键优化」 ----------
    gen_btn.invoke()
    root.update_idletasks(); root.update()

    # 断线断言 ①a：验证面板文本区被自动带入优化阶段的本次材料（show_candidate 触发 sync）
    got_input = verify_input.get("1.0", "end-1c")
    check("断线①a 验证面板文本区自动带入优化阶段「本次材料」",
          got_input == "订单#20260830-1 显示已签收，但用户没收到货",
          f"got={got_input!r}")

    # 断线断言 ②：业务背景保留/同步（上下文文本区仍是优化时那份）
    got_ctx = ctx_text.get("1.0", "end-1c")
    check("断线② 验证时「业务背景」为优化时那份",
          "母婴电商" in got_ctx and "新手妈妈" in got_ctx,
          f"got={got_ctx!r}")

    # 候选版本已显示
    cand_text = text_after_label(window, "候选版本")
    if cand_text is not None:
        got_cand = cand_text.get("1.0", "end-1c")
        check("候选版本已生成显示", "【候选】" in got_cand, f"got={got_cand!r}")

    # ---------- 点击「运行比较」 ----------
    run_btn = None
    for b in find_buttons(window):
        if b.cget("text") == "运行比较":
            run_btn = b
    check("定位「运行比较」按钮", run_btn is not None)
    if run_btn is not None:
        run_btn.invoke()
        root.update_idletasks(); root.update()

    # 成对验证输出：运行比较后出现两个 disabled Text（标题带动态 ms）
    paired_disabled = [t for t in find_texts(window) if node_state(t) == "disabled" and "输出 [" in t.get("1.0", "end-1c")]
    check("成对验证·基线+候选输出均已生成", len(paired_disabled) >= 2,
          f"found={len(paired_disabled)}")

    # ── 运行比较后回查控制器：断线最终证据 ──
    cases = wb.get_pairwise_cases()
    check("断线①b 案例.user_input == 优化阶段本次材料",
          cases and cases[0].get("user_input") == "订单#20260830-1 显示已签收，但用户没收到货",
          f"cases={cases}")
    if cases:
        run_rec = cases[0].get("run")
        check("断线①c 运行记录 user_input 为优化阶段那份",
              run_rec is not None and run_rec.get("user_input") == "订单#20260830-1 显示已签收，但用户没收到货",
              f"run={run_rec}")
        check("断线①d 运行记录 context_text 为优化阶段业务背景",
              run_rec is not None and "母婴电商" in (run_rec.get("context_text") or ""),
              f"run={run_rec}")

    # ---------- 命名断言：用户可见标签 ----------
    has_trigger = any(isinstance(w, tk.Label) and "触发输入" in w.cget("text") for w in all_widgets(window))
    check("命名③ 用户可见标签无「触发输入」", not has_trigger)
    has_ctx_old = any(isinstance(w, tk.Label) and "业务上下文" in w.cget("text") for w in all_widgets(window))
    check("命名④ 用户可见标签无「业务上下文」", not has_ctx_old)
    has_ctx_new = any(isinstance(w, tk.Label) and w.cget("text") == "业务背景" for w in all_widgets(window))
    check("命名⑤ 用户可见标签出现「业务背景」", has_ctx_new)
    has_msg_new = any(isinstance(w, tk.Label) and "本次材料" in w.cget("text") for w in all_widgets(window))
    check("命名⑥ 用户可见标签出现「本次材料」", has_msg_new)

    root.destroy()
    return


if __name__ == "__main__":
    try:
        run()
    except Exception:
        traceback.print_exc()
        print("FATAL_EXCEPTION")
        sys.exit(1)

    print(f"\n===== 结果：PASS {len(RESULTS['ok'])} / FAIL {len(RESULTS['fail'])} =====")
    if RESULTS["fail"]:
        sys.exit(2)
