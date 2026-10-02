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
import io
import glob
import time
import tempfile
from concurrent.futures import ThreadPoolExecutor, as_completed

import pymupdf as fitz
import urllib.request
import urllib.error
from PIL import Image, ImageOps
from dotenv import load_dotenv

load_dotenv()
NIM_BASE = os.environ.get("NIM_ENDPOINT", "https://integrate.api.nvidia.com/v1")
ENDPOINT = NIM_BASE.rstrip("/") + "/chat/completions"
APIKEY = os.environ.get("NIM_API_KEY", "")
MODEL = os.environ.get("NIM_MODEL", "nvidia/nemotron-parse")
PDFDIR = os.environ.get("PDF_DIR", "./pdfs")
OUTDIR = os.environ.get("OUT_DIR", os.path.join(PDFDIR, "md"))
WORKERS = int(os.environ.get("WORKERS", "10"))
DPI = int(os.environ.get("DPI", "200"))
# 扫描件图像预处理：先按 UPSCALE 放大消除锯齿，再二值化去掉 JPEG 压缩噪点。
# 对低分辨率扫描件（有效 DPI < 200）可显著提升识别准确率。
UPSCALE = int(os.environ.get("UPSCALE", "2"))
Binarize = os.environ.get("BINARIZE", "1") not in ("0", "false", "False")
THRESHOLD = int(os.environ.get("THRESHOLD", "160"))
MAX_TOKENS = 4096
RETRIES = 4


def build_markdown(args_json):
    """arguments 形如 [[{bbox,text,type}, ...]]，转成带层级的 Markdown。"""
    try:
        blocks = json.loads(args_json)[0]
    except Exception as e:
        raise ValueError(f"无法解析 tool_calls.arguments: {e}")
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


def _preprocess(img):
    """按 UPSCALE 放大 + 可选二值化，压掉扫描件 JPEG 压缩噪点。"""
    if UPSCALE > 1:
        img = img.resize((img.width * UPSCALE, img.height * UPSCALE), Image.LANCZOS)
    if Binarize:
        img = ImageOps.autocontrast(img).point(lambda x: 255 if x > THRESHOLD else 0)
    return img


def render_page(pdf_path, page_idx):
    """渲染指定页为PNG。

    扫描件（无文本层）优先直接提取内嵌原图：内嵌图即扫描时的真实分辨率，
    避免按 DPI 重采样引入插值伪影——这正是中文识别错字的主因。
    """
    png = os.path.join(tempfile.gettempdir(),
                       f"_np_{os.getpid()}_{page_idx}_{int(time.time()*1000)}.png")
    doc = fitz.open(pdf_path)
    try:
        page = doc[page_idx]
        imgs = page.get_images(full=True)
        native = None
        if not page.get_text().strip() and imgs:
            # 扫描件：取面积最大的内嵌图
            best = max(imgs, key=lambda im: im[2] * im[3])
            native = doc.extract_image(best[0])["image"]
        if native:
            im = Image.open(io.BytesIO(native)).convert("L")
            _preprocess(im).save(png)
        else:
            # 矢量PDF / 无内嵌图：按 DPI 渲染
            page.get_pixmap(dpi=DPI).save(png)
    finally:
        doc.close()
    return png


class EmptyResultError(RuntimeError):
    """接口正常返回但解析结果为空（偶发），视为可重试失败。"""


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
                md = build_markdown(tc[0]["function"]["arguments"])
            else:
                md = resp["choices"][0]["message"].get("content") or ""
            if md and md.strip():
                return md, None
            # 空结果：接口没报错但没解析出内容，重试
            last = EmptyResultError(f"第{attempt}次返回空结果")
        except EmptyResultError as e:
            last = e
        except Exception as e:
            last = e
        if attempt < RETRIES:
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
    if err is None and md and md.strip():
        with open(out, "w") as f:
            f.write(md)
        return name, page_idx + 1, "ok", dt
    tag = f"err:{type(err).__name__}" if err is not None else "err:unknown"
    return name, page_idx + 1, tag, dt


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
