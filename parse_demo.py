#!/usr/bin/env python3
"""
单页/少量页解析示例（验证接口用）
用法：
  export NIM_API_KEY="sk-xxxx"
  python parse_demo.py
默认解析 ./pdfs 下 lec01 / lec04 / assn01 的第 1 页，输出到同目录 .md
"""
import base64
import json
import os

import pymupdf as fitz
import urllib.request

ENDPOINT = os.environ.get("NIM_ENDPOINT", "https://newapi-jp1.202820.xyz/v1/chat/completions")
APIKEY = os.environ.get("NIM_API_KEY", "")
MODEL = os.environ.get("NIM_MODEL", "nvidia/nemotron-parse")
PDFDIR = os.environ.get("PDF_DIR", "./pdfs")

targets = [
    ("lec01.pdf", 0, "lec01_p1.md"),
    ("lec04.pdf", 0, "lec04_p1.md"),
    ("assn01.pdf", 0, "assn01_p1.md"),
]


def parse_image(png_path):
    with open(png_path, "rb") as f:
        b64 = base64.b64encode(f.read()).decode()
    payload = {
        "model": MODEL,
        "messages": [{"role": "user", "content": [
            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{b64}"}}
        ]}],
        "max_tokens": 4096,
    }
    req = urllib.request.Request(
        ENDPOINT, data=json.dumps(payload).encode(),
        headers={"Authorization": f"Bearer {APIKEY}", "Content-Type": "application/json"})
    resp = json.loads(urllib.request.urlopen(req, timeout=180).read())
    tc = resp["choices"][0]["message"].get("tool_calls")
    if tc:
        blocks = json.loads(tc[0]["function"]["arguments"])[0]
        return "\n".join(b.get("text", "") for b in blocks if b.get("text", "").strip())
    return resp["choices"][0]["message"].get("content") or ""


if __name__ == "__main__":
    if not APIKEY:
        print("✗ 请先设置 NIM_API_KEY")
        raise SystemExit(1)
    png = "/tmp/_demo.png"
    for pdf, pg, out in targets:
        path = os.path.join(PDFDIR, pdf)
        if not os.path.exists(path):
            print(f"跳过(不存在) {pdf}")
            continue
        doc = fitz.open(path)
        doc[pg].get_pixmap(dpi=200).save(png)
        doc.close()
        md = parse_image(png)
        with open(os.path.join(PDFDIR, out), "w") as f:
            f.write(md)
        print(f"OK {pdf} 第{pg+1}页 -> {out} ({len(md)} 字符)")
