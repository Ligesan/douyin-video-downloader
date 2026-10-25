#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
抖音视频提取工具
功能：粘贴抖音分享文案（含 v.douyin.com 短链），自动解析并下载无水印视频。
原理：用本机 Edge 浏览器（无头模式）打开视频页，捕获真实播放地址（web 无水印流），再下载到本地。
用法：可直接双击运行；或 python 抖音视频提取工具.py
"""
import os
import re
import sys
import queue
import time
import threading
import traceback
import tkinter as tk
from tkinter import ttk, filedialog, scrolledtext

APP_NAME = "抖音视频提取工具"
VERSION = "1.1"

# ---------------- 链接提取 ----------------
URL_PATTERNS = [
    re.compile(r"https?://v\.douyin\.com/[A-Za-z0-9_-]+"),
    re.compile(r"https?://(?:www\.)?iesdouyin\.com/share/video/\d+"),
    re.compile(r"https?://(?:www\.)?douyin\.com/(?:share/video/)?video/\d+"),
]

def extract_links(text: str):
    # 去掉零宽字符等不可见字符，防止复制文案里的隐藏字符破坏链接匹配
    text = re.sub(r"[\u200b\u200c\u200d\u2060\ufeff]", "", text)
    links = []
    for pat in URL_PATTERNS:
        for m in pat.finditer(text):
            u = m.group(0).rstrip("/")
            if u not in links:
                links.append(u)
    return links

def collect_links(texts) -> list:
    """兼容传入单段文字(str)或多段文字(list/元组)，统一去重保序。"""
    if isinstance(texts, str):
        texts = [texts]
    links = []
    for t in texts:
        links.extend(extract_links(t))
    seen, out = set(), []
    for l in links:
        if l not in seen:
            seen.add(l)
            out.append(l)
    return out

def sanitize_filename(name: str, max_len: int = 60) -> str:
    name = re.sub(r'[\\/:*?"<>|\r\n\t]', "_", name).strip()
    name = re.sub(r"\s+", " ", name)
    if len(name) > max_len:
        name = name[:max_len]
    return name or "抖音视频"

def unique_path(folder: str, base: str) -> str:
    path = os.path.join(folder, base)
    stem, ext = os.path.splitext(path)
    i = 1
    while os.path.exists(path):
        path = f"{stem}({i}){ext}"
        i += 1
    return path

# ---------------- 抓取引擎 ----------------
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36")

def capture_video(link: str, headed: bool, log, poll_sec: int = 120):
    """打开页面并捕获视频真实地址。
    返回 (best_url, title) 或 (None, title/错误信息)。"""
    from playwright.sync_api import sync_playwright

    title = link
    try:
        with sync_playwright() as p:
            browser = p.chromium.launch(channel="msedge", headless=not headed)
            try:
                ctx = browser.new_context(
                    user_agent=UA,
                    viewport={"width": 1280, "height": 800},
                    locale="zh-CN",
                )
                page = ctx.new_page()
                page.goto(link, timeout=30000, wait_until="domcontentloaded")
                # 等待视频加载
                if headed:
                    log("已在浏览器中打开页面，若需登录请完成登录并播放视频…")
                page.wait_for_timeout(9000)
                try:
                    page.mouse.move(640, 400)
                    page.wait_for_timeout(2000)
                except Exception:
                    pass

                title = page.title().replace(" - 抖音", "").strip() or link

                def collect_candidates():
                    cands = []
                    try:
                        v = page.evaluate(
                            "() => { const v = document.querySelector('video'); "
                            "return v ? (v.currentSrc || v.src) : null }")
                        if v:
                            cands.append(v)
                    except Exception:
                        pass
                    try:
                        perf = page.evaluate(
                            "() => performance.getEntriesByType('resource')"
                            ".map(e => e.name)"
                            ".filter(u => /douyinvod|aweme\\/v1\\/play/.test(u))")
                        cands.extend(perf)
                    except Exception:
                        pass
                    return cands

                def pick_best(cands):
                    best, best_br = None, -1
                    for u in cands:
                        if u.startswith("blob:"):
                            continue
                        m = re.search(r"br=(\d+)", u)
                        br = int(m.group(1)) if m else 0
                        if br > best_br:
                            best_br, best = br, u
                    return best

                url = pick_best(collect_candidates())

                # 无头失败 → 改用可见窗口，轮询等待用户登录/播放
                if not url and not headed:
                    ctx.close()
                    ctx = browser.new_context(
                        user_agent=UA,
                        viewport={"width": 1280, "height": 800},
                        locale="zh-CN",
                    )
                    page = ctx.new_page()
                    page.goto(link, timeout=30000, wait_until="domcontentloaded")
                    log("未自动捕获到视频。若该视频需要登录，请在窗口中登录并播放…")
                    deadline = time.time() + poll_sec
                    while time.time() < deadline:
                        page.wait_for_timeout(2000)
                        url = pick_best(collect_candidates())
                        if url:
                            break
                elif not url and headed:
                    # 可见模式下继续轮询
                    deadline = time.time() + poll_sec
                    while time.time() < deadline:
                        page.wait_for_timeout(2000)
                        url = pick_best(collect_candidates())
                        if url:
                            break

                ctx.close()
                return url, title
            finally:
                browser.close()
    except Exception as e:
        log(f"浏览器出错: {e}")
        return None, title


def download(url: str, title: str, folder: str, log, progress_cb=None) -> str:
    import requests
    headers = {"User-Agent": UA, "Referer": "https://www.douyin.com/"}
    with requests.get(url, headers=headers, stream=True, timeout=60) as r:
        if r.status_code != 200:
            raise RuntimeError(f"下载失败 HTTP {r.status_code}")
        total = int(r.headers.get("Content-Length") or 0)
        fname = sanitize_filename(title) + ".mp4"
        path = unique_path(folder, fname)
        done = 0
        with open(path, "wb") as f:
            for chunk in r.iter_content(1 << 20):
                if chunk:
                    f.write(chunk)
                    done += len(chunk)
                    if total and progress_cb:
                        progress_cb(done / total)
        return path


# ---------------- 工作线程 ----------------
class Worker(threading.Thread):
    def __init__(self, texts, folder, headed, log, progress, done_cb):
        super().__init__(daemon=True)
        self.texts = texts
        self.folder = folder
        self.headed = headed
        self.log = log
        self.progress = progress
        self.done_cb = done_cb

    def run(self):
        try:
            links = collect_links(self.texts)

            if not links:
                self.log("⚠️ 未在输入内容中找到抖音链接（需包含 v.douyin.com 或 douyin.com/video/ 链接）")
                return
            self.log(f"共识别到 {len(links)} 条链接，开始处理…")
            ok, fail = 0, []
            for i, link in enumerate(links):
                self.log(f"\n[{i+1}/{len(links)}] {link}")
                self.progress(i / len(links))
                url, title = capture_video(link, self.headed, self.log)
                if not url:
                    msg = "未捕获到视频（可能需登录、是图文作品或视频不可用）"
                    self.log(f"   ❌ {msg}")
                    fail.append(link)
                    continue
                self.log(f"   已获取播放地址，开始下载…")
                try:
                    path = download(url, title, self.folder, self.log,
                                    lambda f, i=i: self.progress(i / len(links) + f / len(links)))
                    size_mb = os.path.getsize(path) / 1048576
                    self.log(f"   ✅ 已保存: {os.path.basename(path)}  ({size_mb:.1f} MB)")
                    ok += 1
                except Exception as e:
                    self.log(f"   ❌ 下载失败: {e}")
                    fail.append(link)
            self.progress(1.0)
            if fail:
                self.log(f"\n完成：成功 {ok} 条，失败 {len(fail)} 条。")
            else:
                self.log(f"\n全部完成！成功下载 {ok} 条，保存在: {self.folder}")
        except Exception:
            self.log("程序出错:\n" + traceback.format_exc())
        finally:
            self.done_cb()


# ---------------- GUI ----------------
class App:
    def __init__(self, root):
        self.root = root
        root.title(f"{APP_NAME} v{VERSION}")
        root.geometry("760x640")
        root.minsize(680, 560)

        self.msg_q = queue.Queue()
        self.worker = None
        self.folder = os.path.join(os.path.expanduser("~"), "Downloads")

        pad = {"padx": 12, "pady": 6}
        frm = ttk.Frame(root, padding=12)
        frm.pack(fill="both", expand=True)

        ttk.Label(frm, text="抖音视频提取工具：粘贴分享文案（可一次粘贴多条），自动识别链接并下载无水印视频。",
                  font=("Microsoft YaHei UI", 10)).pack(anchor="w", **pad)

        self.txt = scrolledtext.ScrolledText(frm, height=7, font=("Consolas", 10), wrap="word")
        self.txt.pack(fill="x", **pad)

        row = ttk.Frame(frm)
        row.pack(fill="x", **pad)
        ttk.Label(row, text="保存到:").pack(side="left")
        self.folder_var = tk.StringVar(value=self.folder)
        e = ttk.Entry(row, textvariable=self.folder_var, width=50)
        e.pack(side="left", fill="x", expand=True, padx=6)
        ttk.Button(row, text="选择文件夹", command=self.pick_folder).pack(side="left")

        self.headed_var = tk.BooleanVar(value=False)
        ttk.Checkbutton(frm, text="遇到需要登录的视频时，弹出浏览器窗口手动登录（较慢但更稳）",
                        variable=self.headed_var).pack(anchor="w", **pad)

        row2 = ttk.Frame(frm)
        row2.pack(fill="x", **pad)
        self.btn = ttk.Button(row2, text="🚀 解析并下载", command=self.start)
        self.btn.pack(side="left")
        self.pbar = ttk.Progressbar(row2, maximum=1.0, value=0.0)
        self.pbar.pack(side="left", fill="x", expand=True, padx=10)

        ttk.Label(frm, text="处理日志:").pack(anchor="w", **pad)
        self.log_box = scrolledtext.ScrolledText(frm, height=12, font=("Consolas", 9), state="disabled")
        self.log_box.pack(fill="both", expand=True, **pad)

        self.poll()

    def pick_folder(self):
        d = filedialog.askdirectory(initialdir=self.folder_var.get() or os.path.expanduser("~"))
        if d:
            self.folder_var.set(d)

    def log(self, msg: str):
        self.msg_q.put(msg)

    def poll(self):
        try:
            while True:
                msg = self.msg_q.get_nowait()
                self.log_box.configure(state="normal")
                self.log_box.insert("end", msg + "\n")
                self.log_box.see("end")
                self.log_box.configure(state="disabled")
        except queue.Empty:
            pass
        self.root.after(100, self.poll)

    def start(self):
        if self.worker and self.worker.is_alive():
            self.log("正在处理中，请稍候…")
            return
        texts = self.txt.get("1.0", "end")
        folder = self.folder_var.get().strip()
        if not texts.strip():
            self.log("请先粘贴抖音分享文案或链接。")
            return
        if not os.path.isdir(folder):
            try:
                os.makedirs(folder, exist_ok=True)
            except Exception:
                folder = os.path.join(os.path.expanduser("~"), "Downloads")
                self.folder_var.set(folder)
        self.pbar["value"] = 0.0
        self.btn.configure(state="disabled")
        self.log("—" * 40)
        self.worker = Worker(texts, folder, self.headed_var.get(), self.log,
                             lambda v: self.pbar.configure(value=v),
                             lambda: self.btn.configure(state="normal"))
        self.worker.start()


def cli_run(text_file: str, out_folder: str) -> int:
    """命令行模式：读取文本文件中的分享文案，批量下载。用于自检/高级用法。"""
    import requests  # noqa
    with open(text_file, "r", encoding="utf-8") as f:
        texts = [f.read()]
    os.makedirs(out_folder, exist_ok=True)
    report = os.path.join(out_folder, "cli_report.txt")
    ok, fail = 0, 0
    try:
        links = collect_links(texts)
        for i, link in enumerate(links):
            url, title = capture_video(link, False, lambda m: None)
            if not url:
                fail += 1
                continue
            try:
                download(url, title, out_folder, lambda m: None)
                ok += 1
            except Exception:
                fail += 1
    except Exception:
        with open(report, "w", encoding="utf-8") as f:
            f.write("RESULT: ERROR\n" + traceback.format_exc())
        return 1
    with open(report, "w", encoding="utf-8") as f:
        f.write(f"RESULT: OK ok={ok} fail={fail}\n")
    return 0 if fail == 0 else 2


def main():
    if len(sys.argv) >= 3 and sys.argv[1] == "--cli":
        sys.exit(cli_run(sys.argv[2], sys.argv[3]))
    root = tk.Tk()
    App(root)
    root.mainloop()


if __name__ == "__main__":
    main()
