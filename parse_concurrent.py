#!/usr/bin/env python3
"""
nvidia-nim-parse 并发文档解析工具
=================================
用 NVIDIA NIM 的 `nvidia/nemotron-parse` (v1) 把 PDF 每页渲染成图片后解析为 Markdown。

特性：
  - 多页并发请求（默认 10 路），充分利用 NIM 接口的独立网络 I/O
  - 失败自动重试（SSL 中断 / 5xx  transient）
  - 断点续跑：已生成且非空的 .md 自动跳过

用法：
  export NIM_API_KEY="sk-xxxx"            # 必填，NIM 端点 key
  python parse_concurrent.py             # 解析 ./pdfs 下所有 PDF -> ./pdfs/md

可用环境变量：
  NIM_API_KEY    NIM 接口 key（必填）
  NIM_ENDPOINT   chat completions 地址（默认 https://newapi-jp1.202820.xyz/v1/chat/completions）
  NIM_MODEL      模型名（默认 nvidia/nemotron-parse）
  PDF_DIR        输入 PDF 目录（默认 ./pdfs）
  OUT_DIR        输出 Markdown 目录（默认 ./pdfs/md）
  WORKERS        并发数（默认 10）
  DPI            渲染分辨率（默认 200）
"""
import base64
import json
import os
import glob
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import pymupdf as fitz
import urllib.request
import urllib.error

ENDPOINT = os.environ.get("NIM_ENDPOINT", "https://newapi-jp1.202820.xyz/v1/chat/completions")
APIKEY = os.environ.get("NIM_API_KEY", "")
MODEL = os.environ.get("NIM_MODEL", "nvidia/nemotron-parse")
PDFDIR = os.environ.get("PDF_DIR", "./pdfs")
OUTDIR = os.environ.get("OUT_DIR", os.path.join(PDFDIR, "md"))
WORKERS = int(os.environ.get("WORKERS", "10"))
DPI = int(os.environ.get("DPI", "200"))
MAX_TOKENS = 4096
RETRIES = 4


def build_markdown(args_json):
    """arguments 形如 [[{bbox,text,type}, ...]]，转成带层级的 Markdown。"""
    try:
        blocks = json.loads(args_json)[0]
    except Exception:
        return ""
    lines = []
    for b in blocks:
        t, txt = b.get("type"), (b.get("text") or "").strip()
        if not txt:
            continue
        if t == "Section-header":
            lines.append(f"# {txt}")
        elif t == "Title":
            lines.append(f"## {txt}")
        elif t in ("Page-header", "Page-footer"):
            lines.append(f"<!-- {t}: {txt} -->")
        else:
            lines.append(txt)
    return "\n\n".join(lines)


def render_page(pdf_path, page_idx):
    png = os.path.join(tempfile.gettempdir(),
                       f"_np_{os.getpid()}_{page_idx}_{int(time.time()*1000)}.png")
    doc = fitz.open(pdf_path)
    doc[page_idx].get_pixmap(dpi=DPI).save(png)
    doc.close()
    return png


def call_parse(png_path):
    if not APIKEY:
        raise RuntimeError("未设置 NIM_API_KEY 环境变量")
    with open(png_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
        ]}],
        "max_tokens": MAX_TOKENS,
    }
    last = None
    for attempt in range(1, RETRIES + 1):
        try:
            req = urllib.request.Request(
                ENDPOINT, data=json.dumps(payload).encode(),
                headers={"Authorization": f"Bearer {APIKEY}", "Content-Type": "application/json"})
            resp = json.loads(urllib.request.urlopen(req, timeout=240).read())
            tc = resp["choices"][0]["message"].get("tool_calls")
            if tc:
                return build_markdown(tc[0]["function"]["arguments"]), None
            return resp["choices"][0]["message"].get("content") or "", None
        except Exception as e:
            last = e
            time.sleep(2 * attempt)
    return None, last


def process(pdf_path, page_idx, name):
    out = os.path.join(OUTDIR, f"{name}_p{page_idx+1:03d}.md")
    if os.path.exists(out) and os.path.getsize(out) > 0:   # 断点续跑
        return name, page_idx + 1, "skip", 0
    png = render_page(pdf_path, page_idx)
    t0 = time.time()
    md, err = call_parse(png)
    try:
        os.remove(png)
    except OSError:
        pass
    dt = time.time() - t0
    if err is None and md is not None:
        with open(out, "w") as f:
            f.write(md)
        return name, page_idx + 1, "ok", dt
    return name, page_idx + 1, f"err:{type(err).__name__}", dt


def main():
    if not APIKEY:
        print("✗ 请先设置环境变量 NIM_API_KEY")
        return
    os.makedirs(OUTDIR, exist_ok=True)
    tasks = []
    for pdf in sorted(glob.glob(os.path.join(PDFDIR, "*.pdf"))):
        name = os.path.splitext(os.path.basename(pdf))[0]
        n = fitz.open(pdf).page_count
        for i in range(n):
            tasks.append((pdf, i, name))
    total = len(tasks)
    print(f"待解析页数: {total}  并发: {WORKERS}  模型: {MODEL}", flush=True)
    done = ok = skip = err = 0
    t_start = time.time()
    with ThreadPoolExecutor(max_workers=WORKERS) as ex:
        futs = [ex.submit(process, *t) for t in tasks]
        for fut in as_completed(futs):
            name, pg, status, dt = fut.result()
            done += 1
            if status == "ok":
                ok += 1
            elif status == "skip":
                skip += 1
            else:
                err += 1
                print(f"  ✗ {name} p{pg} {status}", flush=True)
            if done % 20 == 0 or done == total:
                el = time.time() - t_start
                print(f"  进度 {done}/{total}  ok={ok} skip={skip} err={err}  已用 {el/60:.1f}min", flush=True)
    print(f"完成: ok={ok} skip={skip} err={err}  总用时 {(time.time()-t_start)/60:.1f}min", flush=True)


if __name__ == "__main__":
    main()
