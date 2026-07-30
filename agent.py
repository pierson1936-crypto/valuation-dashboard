# -*- coding: utf-8 -*-
"""
估值分析 AI 助手（命令行版）
============================
和网页里的「AI 助手」用的是同一套引擎（都在 app.py 里）。这个脚本只是它的
命令行外壳，适合喜欢在终端里对话的人。网页版更直观（右下角那个按钮）。

用法：
  1) 申请 DeepSeek Key： https://platform.deepseek.com
  2) 设环境变量 DEEPSEEK_API_KEY，或直接把 Key 填进下面 KEY = ""
  3) python agent.py   然后对话，exit 退出

免责声明：数据由公开接口抓取、结论按规则计算，仅供研究，不构成投资建议。
"""
import os
import sys

import app  # 复用 app.py 里的 agent_run / 工具 / 分析引擎

KEY = os.environ.get("DEEPSEEK_API_KEY", "") or ""   # 也可把 Key 直接写这里


def main():
    if not KEY:
        print("!! 未设置 DEEPSEEK_API_KEY。请设为环境变量，或填到 agent.py 顶部的 KEY。")
        print("   申请： https://platform.deepseek.com")
        sys.exit(1)
    print("=" * 56)
    print("  估值分析 AI 助手（命令行）  模型：%s" % app.AGENT_MODEL)
    print("  例如： 茅台贵不贵 / 600519和000858哪个便宜 / 从这几只挑风险最低的")
    print("  输入 exit 退出。")
    print("=" * 56)
    history = []
    while True:
        try:
            user = input("\n你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n再见。"); break
        if user.lower() in ("exit", "quit", "q", "退出"):
            print("再见。"); break
        if not user:
            continue
        history.append({"role": "user", "content": user})
        try:
            reply, trace, history = app.agent_run(history, KEY)
        except Exception as e:
            print("调用失败：%s（检查网络/Key/余额）" % e)
            history.pop()
            continue
        for t in trace:
            print("   🔧 %s(%s)" % (t["tool"], ", ".join("%s=%s" % kv for kv in t["args"].items())))
        print("\n助手：" + reply)


if __name__ == "__main__":
    main()
